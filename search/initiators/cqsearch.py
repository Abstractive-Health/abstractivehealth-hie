import asyncio
import boto3
import json
import os
from datetime import datetime, timezone

import psycopg2
from patient_metadata import PatientMetadata
from pipeline import Pipeline

import utils

ENV = os.environ.get("ENV")

DB_HOST_NAME = f''
secretsmanager = boto3.client('secretsmanager')
secret_id = f""
secret_params = json.loads(secretsmanager.get_secret_value(SecretId=secret_id)["SecretString"])

cqcert_https = secret_params['cqcert_https']
cqkey_https = secret_params['cqkey_https']
trusted_https = secret_params['trusted_https']
with open('/tmp/cqcert.crt', 'w') as f:
    f.write(cqcert_https)
with open('/tmp/cqkey.key', 'w') as f:
    f.write(cqkey_https)
with open('/tmp/trusted.pem', 'w') as f:
    f.write(trusted_https)

MAX_PARALLEL_REQUESTS = 200
ROUNDS_OF_REQUESTS = 1


class CQSearch:
    def __init__(
            self,
            responders,
            patient_metadatas,
            user_qualifications,
            org_db_name=None,
            national_endpoints=[],
            for_api=False):
        self.user_qualifications = user_qualifications

        self.app_connection = psycopg2.connect(
            host=DB_HOST_NAME,
            port=5432,
            user=secret_params['db_username'],
            password=secret_params['db_password'],
            database=org_db_name
        )
        self.app_connection.autocommit = True
        self.for_api = for_api
        self.pipelines = []
        self.remaining_pipelines = []
        self.patient_metadata_objs = [PatientMetadata(
            patient_metadata) for patient_metadata in patient_metadatas]

        for patient_metadata_obj in self.patient_metadata_objs:
            # get responders that correspond to this address's zip
            # this field ought to always be not null, per IDI lambda
            relevant_responders = responders[patient_metadata_obj.postal_code]
            self.pipelines.extend([
                Pipeline(
                    responder['name'],
                    responder['oid'],
                    responder['iti55_responder'],
                    responder['iti38_responder'],
                    responder['iti39_responder'],
                    self.user_qualifications,
                    self.app_connection,
                    patient_metadata_obj
                )
                for responder in relevant_responders + national_endpoints
            ])

        self.patients_found = []
        self.internal_additions_v1 = {"pid": None, "doc_ids": []}

    async def gather_55_pipelines(self):

        results = []
        for round in range(ROUNDS_OF_REQUESTS):
            print("iti 55 round", round)
            results.extend(await asyncio.gather(
                *[pipeline.initiate_xcpd_with_patient_metadata()
                  for pipeline in self.pipelines[round * MAX_PARALLEL_REQUESTS:(round + 1) * MAX_PARALLEL_REQUESTS]]
            ))
        return results

    def collect_all_possible_patients(self):
        all_found_metadata = asyncio.run(self.gather_55_pipelines())
        self.patients_found = [
            {
                "pipeline": pipeline.name,
                "pipeline_oid": pipeline.oid,
                "patient_metadata": found_metadata
            }
            for pipeline, found_metadata in zip(self.pipelines, all_found_metadata)
        ]
        return self.patients_found.copy()

    def collect_55_latencies(self):
        '''
        return a list of latencies for iti 55
        '''
        self.iti55latencies = [{
            "pipeline": pipeline.name,
            "pipeline_oid": pipeline.oid,
            "pipeline_latency": pipeline.iti55latency
        } for pipeline in self.pipelines]
        return self.iti55latencies.copy()

    def conflict_checker_dedup(self):
        '''
        check for conflicts between returns (currently unimplemented, TODO)
        as well as duplicated endpoints.

        an example of duplicated endpoints is if a person lived in 2 close-by zips
        and the same endpoint is contained in both of those zipcode neighbors
        and both return he is found
        In this scenario, we don't want to skip querying one of the endpoints,
        because it's with a different set of metadata (address, albeit close-by).
        So we will deduplicate after.

        another example is national endpoints being queried twice with different metadata,
        but both returning found
        '''

        names_oids = set()
        for i, iti55ret in enumerate(self.patients_found):
            if type(iti55ret["patient_metadata"]) is str:
                # "NF", "Timeout", or "Multiple"
                continue
            else:
                if iti55ret["pipeline"] + iti55ret["pipeline_oid"] in names_oids:
                    # duplicated successful endpoint
                    continue
                else:
                    names_oids.add(iti55ret["pipeline"] + iti55ret["pipeline_oid"])
                    self.remaining_pipelines.append(self.pipelines[i])

        return

    def pipelines_with_patient_found(self) -> bool:
        '''
        for FE, return a list of pipelines that have found a patient after conflict checking
        for cq external, return a dict counter of oid: count of patients found'''

        counter = {}
        for pipeline in self.remaining_pipelines:
            if pipeline.oid not in counter:
                counter[pipeline.oid] = 1
            else:
                counter[pipeline.oid] += 1

        return [pipeline.name for pipeline in self.remaining_pipelines], counter

    async def gather_38_39_pipelines(self):
        print("remaining pipelines", self.remaining_pipelines)
        return await asyncio.gather(*[pipeline.get_docs() for pipeline in self.remaining_pipelines])

    def find_docs_for_conflict_free_patients(self, conversation_id=None):
        '''
        queries for, and inserts docs for pipelines that have found a patient in 55
        docs will end up in cq_notes
        '''
        print("in here, find_docs_for_conflict_free_patients")
        all_retrieved_xmls_by_loinc = asyncio.run(self.gather_38_39_pipelines())

        self.all_additions_in_db = [
            {"pipeline": pipeline.name,
             "docs": docs[0],
             "fhir_id": docs[1]}
            for pipeline, docs
            in zip(self.remaining_pipelines, all_retrieved_xmls_by_loinc)]

        cur = self.app_connection.cursor()

        db_v1_return = None
        if not conversation_id:
            # redacted: insertion
            try:
                self.internal_additions_v1["doc_ids"].extend(list(set_note_ids_v1))
                db_v1_return = self.internal_additions_v1.copy()

            except Exception as e:
                print(e)
                print("pass v1 write in cq search")
        else:  # write into the api table
            # API WRITE START
            api_insert_query = """
                redacted
                """
            clean = [{"pipeline": addition["pipeline"],
                      "docs": addition["docs"]}
                     for addition in self.all_additions_in_db]
            contents = (utils.clean_json_for_postgres(clean),
                        str(datetime.now(timezone.utc)),
                        conversation_id)
            cur.execute(api_insert_query, contents)
            cur.close()
            print("api wrote for conversation id", conversation_id)
            # API WRITE END
            return None
        self.app_connection.close()
        # TODO: return v1 only

        return db_v1_return

    def collect_38_39_latencies(self):
        '''
        return a list of latencies for iti 38 and 39
        '''
        self.iti38latencies = [{
            "pipeline": pipeline.name,
            "pipeline_oid": pipeline.oid,
            "pipeline_latency": pipeline.iti38latency
        } for pipeline in self.remaining_pipelines]

        self.iti39latencies = [{
            "pipeline": pipeline.name,
            "pipeline_oid": pipeline.oid,
            "pipeline_latency": pipeline.iti39latency
        } for pipeline in self.remaining_pipelines]

        return self.iti38latencies.copy(), self.iti39latencies.copy()

    def counter_found_docs(self):
        '''
        return a dict counter of oid: count of docs found
        '''
        counter = {}
        for pipeline in self.remaining_pipelines:
            if pipeline.oid not in counter:
                counter[pipeline.oid] = pipeline.num_found_docs
            else:
                counter[pipeline.oid] += pipeline.num_found_docs
        return counter
