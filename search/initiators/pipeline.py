import asyncio
import random
import re
import statistics
from typing import List, Tuple, Union

import fhirbase
from iti38initiator import ITI38Initiator
from iti39initiator import ITI39Initiator
from iti55initiator import ITI55Initiator
from lxml import etree
from patient_metadata import PatientMetadata

from utils import extract_envelope_content


class Pipeline:
    def __init__(
            self,
            name,
            oid,
            url55resp,
            url38resp,
            url39resp,
            user_qualifications,
            connection,
            patient_metadata,
            iti55_random_wait=90,
            iti55_timeout=45,
            iti38_timeout=60,
            iti39_timeout=60,
            iti39_batch_size=5) -> None:
        self.name = name
        self.oid = oid
        self.url55resp = url55resp
        self.url38resp = url38resp
        self.url39resp = url39resp

        self.user_qualifications = user_qualifications
        for key, value in user_qualifications.items():
            if value == None:
                raise Exception("user missing qualification field,", key)

        # each target has one connection to db, and it needs to be closed after the pipeline is discarded or completed
        self.iti55initiator = None
        self.iti55latency = None
        self.iti55_random_wait = iti55_random_wait
        self.iti55_timeout = iti55_timeout

        self.iti38initiator = None
        self.iti38latency = None
        self.iti38_timeout = iti38_timeout

        self.iti39initiators = []  # at most 10 doc requests per iti39, so need more than 1 initiators
        self.iti39latency = None
        self.iti39_timeout = iti39_timeout
        self.iti39_batch_size = iti39_batch_size

        self.received_55_response = None  # raw, non-preprocessed strings of response.text
        self.received_38_response = None
        self.received_39_responses = []

        # for 55
        self.patient_metadata = patient_metadata

        # for 38
        self.patient_ids = []  # list of (patient_root, patient_extension)
        self.return_type = "LeafClass"  # can be overridden to ObjectRef, but ObjectRef is mostly useless
        # a place to put a filter on the documents we want to process; if None, we process all documents. maybe size and type (fhir or ccd)
        self.doc_filter = None

        # for 39
        # list of {"pid": patient_id, "doc_id": document_unique_id, "rid": repository_id_for_doc}
        self.pids_and_doc_ids = []

        # for fhir converter
        self.docs_found = {"converted_fhir": []}
        self.num_found_docs = 0

        # after fhir converter, to record where the docs are inserted
        # self.db_pid = None
        # self.db_doc_ids = None

        self.fb = fhirbase.FHIRBase(connection)

    def __str__(self) -> str:
        return f"object with OID: {self.oid}"

    async def initiate_xcpd_with_patient_metadata(self) -> Union[Tuple, str, None]:
        # translate from patient_metadata to params for ITI55
        # use iti55 initiator
        await asyncio.sleep(random.randrange(self.iti55_random_wait))
        try:
            self.iti55initiator = ITI55Initiator(
                params=self.patient_metadata.get_dict_for_iti55(),
                responder_url=self.url55resp,
                responder_hcid=self.oid,
                user_qualifications=self.user_qualifications,
                timeout=self.iti55_timeout
            )
            self.received_55_response = await self.iti55initiator.send_request()  # set this to the response.text
            # post-process to get patient metadata as returned from 55, to prepare for conflict checking
            # also get one pair of patient_root, patient id and set self.patient_ids
            found_patient = self.extract_patient_metadata_and_pid()[0]
            self.iti55latency = self.iti55initiator.request_latency
        except:
            print("exception in initiate_xcpd_with_patient_metadata")
            self.received_55_response = None
            self.iti55latency = None
            return "Timeout"
        return found_patient  # either as a string, "NF", "Multiple", "Timeout", or a PatientMetadata object

    def extract_patient_metadata_and_pid(self) -> Union[Tuple[PatientMetadata, List],
                                                        Tuple[str, str]]:
        # if one found, organize the metadata in a dict. if none found, set to "NF". if multiple found, set to "Multiple"
        # parse the response
        preparsed = self.received_55_response
        try:
            if preparsed is None:
                return "Timeout", ""
            if preparsed == "" or extract_envelope_content(preparsed) is None:
                return "NF", ""

            try:
                response_tree = etree.fromstring(extract_envelope_content(preparsed))
            except:
                print("unable to make a tree out of the envelope")
                print("preparsed", preparsed)
                return "NF", ""

            # extract queryResponseCode and validates that it says "OK" which means at least one patient found
            qrc_element = response_tree.find('.//{*}queryResponseCode')
            if qrc_element is None:
                return "NF", ""
            query_response_code = qrc_element.attrib['code']

            if query_response_code != 'OK':
                return "NF", ""
            else:  # one or multiple
                # if one registrationEvent, we have one patient
                # if multiple registrationEvents, we have multiple patients all fitting
                # if zero registrationEvents, we have multiple patients all possible but not fitting
                registration_events = response_tree.findall('.//{*}registrationEvent')
                if not registration_events or len(registration_events) == 0:
                    # > 1 close matches with 0 exact match
                    return "NF", ""
                elif len(registration_events) == 1:
                    patient = registration_events[0].find('.//{*}patient')
                    patient_metadata_dict = {}
                    patient_root = patient.find('.//{*}id').attrib['root']
                    patient_id_ext = patient.find('.//{*}id').attrib['extension']

                    try:
                        patient_metadata_dict['given_name'] = patient.find('.//{*}given').text
                    except AttributeError:
                        patient_metadata_dict['given_name'] = None

                    try:
                        patient_metadata_dict['family_name'] = patient.find('.//{*}family').text
                    except AttributeError:
                        patient_metadata_dict['family_name'] = None

                    try:
                        patient_metadata_dict['administrative_gender_code'] = patient.find(
                            './/{*}administrativeGenderCode').attrib['code']
                    except (AttributeError, KeyError):
                        patient_metadata_dict['administrative_gender_code'] = None

                    try:
                        patient_metadata_dict['birth_time'] = patient.find(
                            './/{*}birthTime').attrib['value']
                    except (AttributeError, KeyError):
                        patient_metadata_dict['birth_time'] = None

                    try:
                        patient_metadata_dict['phone_number'] = patient.find(
                            './/{*}telecom').attrib['value']
                    except (AttributeError, KeyError):
                        patient_metadata_dict['phone_number'] = None

                    patient_address = patient.find('.//{*}addr')

                    try:
                        patient_metadata_dict['street_address_line'] = patient_address.find(
                            './/{*}streetAddressLine').text
                    except AttributeError:
                        patient_metadata_dict['street_address_line'] = None

                    try:
                        patient_metadata_dict['city'] = patient_address.find('.//{*}city').text
                    except AttributeError:
                        patient_metadata_dict['city'] = None

                    try:
                        patient_metadata_dict['state'] = patient_address.find('.//{*}state').text
                    except AttributeError:
                        patient_metadata_dict['state'] = None

                    try:
                        patient_metadata_dict['postal_code'] = patient_address.find(
                            './/{*}postalCode').text
                    except AttributeError:
                        patient_metadata_dict['postal_code'] = None

                    try:
                        patient_metadata_dict['country'] = patient_address.find(
                            './/{*}country').text
                    except AttributeError:
                        patient_metadata_dict['country'] = None

                    self.patient_metadata = PatientMetadata(patient_metadata_dict)
                    self.patient_ids = [(patient_root, patient_id_ext)]
                    return self.patient_metadata, [(patient_root, patient_id_ext)]
                else:
                    return "Multiple", ""
        except Exception as e:
            print("error in extract_patient_metadata_and_pid", e)
            return "NF", ""

    async def get_docs(self):
        # trigger ITI38, ITI39
        iti38params = {"pids": self.patient_ids,  # these are the pids internal to other people's system
                       "returntype": "LeafClass"}
        await (asyncio.sleep(random.randrange(10)))
        try:
            self.iti38initiator = ITI38Initiator(
                params=iti38params, responder_url=self.url38resp, responder_hcid=self.oid,
                user_qualifications=self.user_qualifications, timeout=self.iti38_timeout)
            self.received_38_response = await self.iti38initiator.send_request()
            self.iti38latency = self.iti38initiator.request_latency
        except Exception as e:
            print(f"exception in iti38 creation or request, {e}")
            self.received_38_response = None
            self.iti38latency = None
        print("in get docs, received 38 response", self.received_38_response)
        self.extract_ITI39_params()
        print("pids and doc ids and loincs", self.pids_and_doc_ids)

        # break into chunks of 1 per request. epic complains if > 10 per request. 1 per request also allows for more async
        # unfortunately some endpoints complain with 429, so I'm going with chunks of 5
        for i in range(0, len(self.pids_and_doc_ids), self.iti39_batch_size):
            try:
                iti_39_initiator = ITI39Initiator(
                    params={"pid_and_doc_ids": self.pids_and_doc_ids[i:i+5]},
                    responder_url=self.url39resp,
                    responder_hcid=self.oid,
                    user_qualifications=self.user_qualifications,
                    timeout=self.iti39_timeout)
                self.iti39initiators.append(iti_39_initiator)
            except Exception as e:
                print(f"issue creating iti39 initiator, {e}")
                # this try except was brought to life by this error log https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:log-groups/log-group/$252Faws$252Flambda$252Fprod-ah-useast1-hie-patient/log-events/2024$252F05$252F07$252F$255B$2524LATEST$255Dee0d7bef930649218d151f9094492f85$3Fstart$3D1715096155156$26refEventId$3D38247922349343574172699194757823705471706119218654085254
                await (asyncio.sleep(0.5))
                continue

        self.received_39_responses = await asyncio.gather(*[
            self.iti39initiators[i].send_request()
            for i in range(len(self.iti39initiators))
        ])

        successful_latencies = [iti39.request_latency for iti39 in self.iti39initiators
                                if iti39.request_latency is not None]
        if successful_latencies:
            self.iti39latency = statistics.median(successful_latencies)

        return self.extract_full_docs_and_sort()
        # await self.convert_to_fhir()
        # return the locations of the documents found

    def extract_ITI39_params(self) -> List:
        '''
        parse self.received_38_response to get what's needed for iti39 call
        '''
        self.filter = set([])
        preparsed = self.received_38_response
        preparsed = extract_envelope_content(preparsed)
        if preparsed is None:  # Timed out probably
            return []

        try:
            response_tree = etree.fromstring(preparsed)
        except:
            print("unable to make a tree of the envelope")
            print("preparsed,", preparsed)
            return []

        try:
            # extract a list of extrinsic objects
            extrinsic_objects = response_tree.findall('.//{*}ExtrinsicObject')
            self.pids_and_doc_ids = []
            # for each extrinsic object, get all its classification
            for extrinsic_object in extrinsic_objects:
                object_done = False
                # try filling rid with Slot first
                rid = None
                replacement_hcid = None
                try:
                    # for surescripts docs can be at a different hcid as the patient
                    replacement_hcid = extrinsic_object.attrib['home'].strip("urn:oid:")
                except:
                    replacement_hcid = self.oid
                    print("could not find replacement hcid, using the original one")

                try:
                    slots = extrinsic_object.findall('.//{*}Slot')
                    for slot in slots:
                        if slot.attrib['name'] == "repositoryUniqueId":
                            rid = slot.find('.//{*}ValueList/{*}Value').text
                            break
                except:
                    print("could not find repoUniqueId in slots, continuing")

                classifications = extrinsic_object.findall('.//{*}Classification')
                for classification in classifications:
                    if object_done:
                        break
                    value = classification.find('.//{*}Slot/{*}ValueList/{*}Value')
                    if value is not None and value.text == "2.16.840.1.113883.6.1":  # we're dealing with the loinc classification
                        object_done = True  # this object will not need to be looked further
                        # TODO: check if this is a LOINC recognized by our filter.
                        if classification.attrib['nodeRepresentation']:
                            doc_type = classification.attrib['nodeRepresentation']
                            external_identifiers = extrinsic_object.findall(
                                './/{*}ExternalIdentifier')
                            pid = None
                            doc_id = None
                            for external_identifier in external_identifiers:
                                # uuids are here https://profiles.ihe.net/ITI/TF/Volume3/ch-4.2.html
                                if external_identifier.attrib['identificationScheme'] == 'urn:uuid:58a6f841-87b3-4a3e-92fd-a8ffeff98427':
                                    pid_and_rid = external_identifier.attrib['value']
                                    pid = pid_and_rid.split("^^^")[0]
                                    if rid is None:
                                        rid = pid_and_rid.split("^^^&")[1].split("&")[0]
                                elif external_identifier.attrib['identificationScheme'] == 'urn:uuid:2e82c1f6-a085-4c72-9da3-8640a32e42ab':
                                    doc_id = external_identifier.attrib['value']
                            if pid is not None and doc_id is not None and rid is not None:
                                self.pids_and_doc_ids.append(
                                    {"pid": pid,
                                     "doc_id": doc_id,
                                     "rid": rid,
                                     "type": doc_type,
                                     "replacement_hcid": replacement_hcid
                                     }
                                )
            return self.pids_and_doc_ids.copy()

        except Exception as e:
            print("issue unpacking iti38 response", e)
            return []

    def extract_full_docs_and_sort(self):
        for i in range(len(self.received_39_responses)):
            preparsed = self.received_39_responses[i]
            doc_type = self.pids_and_doc_ids[i]["type"]
            if type(preparsed) != str and type(preparsed) != bytes:
                print("weird type preparsed:", type(preparsed))
                print("content below", preparsed)
                # if timeout, preparsed = None, we will not insert
                continue
            if type(preparsed) == bytes:
                print("got a bytes 39 preparsed response")
                preparsed = preparsed.decode('utf-8')

            # envelope = extract_envelope_content(preparsed)
            # extract all clinical documents
            clinical_documents = re.findall(
                r'<ClinicalDocument.*?</ClinicalDocument>', preparsed, re.DOTALL)

            if len(clinical_documents) == 0:
                print("no clinical documents found in 39 response")
                print("preparsed,", preparsed)

            if doc_type in self.docs_found:
                self.docs_found[doc_type].extend(clinical_documents)
            else:
                self.docs_found[doc_type] = clinical_documents

        try:
            fhir_id = self.pids_and_doc_ids[0]['pid']
        except:
            fhir_id = "fhir id not available"

        self.count_successful_docs()

        return self.docs_found.copy(), fhir_id

    def count_successful_docs(self):
        self.num_found_docs = sum([len(self.docs_found[doc_type]) for doc_type in self.docs_found])
        return self.num_found_docs
