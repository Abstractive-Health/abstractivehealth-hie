import asyncio
import base64
import boto3
import json
import traceback

import psycopg2
import requests
from cqsearch import *
from iti38initiator import ITI38Initiator
from iti39initiator import ITI39Initiator
from iti55initiator import ITI55Initiator
from lxml import etree

import utils
from utils import ENV

secretsmanager = boto3.client('secretsmanager')
secret_id = f"{ENV}-lambda-hie"
secret_params = json.loads(secretsmanager.get_secret_value(SecretId=secret_id)["SecretString"])
GOOGLE_API_KEY = secret_params['google_geo_api_key']

dynamodb = boto3.resource('dynamodb').Table(f"")
sqs = boto3.resource('sqs')
queue = sqs.Queue(f'')
STU3_DIRECTORY_LAMBDA = ''

PATIENT_METADATA_SET_LIMIT = 5


def get_db_connection(database=None):
    return psycopg2.connect(
        host=f'', port=5432,
        user=secret_params['db_username'],
        password=secret_params['db_password'],
        database=database)


def get_endpoints_with_zips(zip_codes, each=1):
    '''
    calls stu3 directory to get active endpoints within radius of zip code
    input is [(zip1, state1), (zip2, state2)...]
    '''
    if type(zip_codes) is set:
        zip_codes = list(zip_codes)
    # convert list of tups of zip+state to list of lists
    zip_codes = [list(tup) for tup in zip_codes]

    body = json.dumps({
        "action": "getEndpoints",
        "params": {
            "zip_codes": zip_codes,
            "each": each
        }
    })
    response = requests.post(STU3_DIRECTORY_LAMBDA, data=body, verify=False)
    return json.loads(response.text)


def get_national_endpoints():
    '''
    calls stu3 directory to get a manually created national endpoints list
    '''
    body = json.dumps({
        "action": "getNationalEndpoints",
        "params": {}
    })
    response = requests.post(STU3_DIRECTORY_LAMBDA, data=body, verify=False)
    return json.loads(response.text)


def get_long_lat_from_zips(zipcodes):
    '''
    get a list of unique long_lat pairs (list len 2) from an iterator of zip codes
    '''
    # zipcodes = tuple(set(zipcodes))
    # try:
    #     conn = get_db_connection('carequality')
    #     cur = conn.cursor()
    #     cur.execute("""SELECT longitude, latitude FROM zipcode_neighbors
    #                 WHERE zipcode IN %s
    #                 AND longitude IS NOT NULL
    #                 AND latitude IS NOT NULL""",
    #                 (zipcodes,))
    #     long_lat_pairs = cur.fetchall()
    #     if long_lat_pairs is None:
    #         long_lat_pairs = []
    #     else:
    #         long_lat_pairs = [list(pair) for pair in long_lat_pairs]
    #     cur.close()
    #     conn.close()
    # except Exception as e:
    #     print("could not get long_lat pairs,", e)
    #     return []
    # return long_lat_pairs
    '''
    calls google geocoding api to get long_lat pairs for each
    '''
    try:
        base_url = "https://maps.googleapis.com/maps/api/geocode/json"
        long_lat_pairs = []

        for zipcode in zipcodes:
            params = {
                "address": zipcode,
                "region": "US",
                "key": GOOGLE_API_KEY,
            }

            response = requests.get(base_url, params=params)
            data = response.json()

            if data['status'] == 'OK':
                location = data['results'][0]['geometry']['location']
                longitude = location['lng']
                latitude = location['lat']
                long_lat_pairs.append([longitude, latitude])
            else:
                print("encountered an issue with google api", data)
                print(data)
                pass
        return long_lat_pairs
    except Exception:
        print("exception with google api", traceback.format_exc().replace('\n', '\r'))
        return []


def make_and_initiate_request(endpoint_type, destination_url, destination_oid, params):
    '''
    makes an endpoint to make first contact with someone else with the params we're curious about
    we must already know the destination url
    '''
    test_user_qualification = {}
    if endpoint_type == '/iti55initiator/p':
        initiator = ITI55Initiator(None, None, params, destination_url,
                                   destination_oid, test_user_qualification)
    elif endpoint_type == '/iti38initiator/p':
        initiator = ITI38Initiator(None, None, params, destination_url,
                                   destination_oid, test_user_qualification)
    elif endpoint_type == '/iti39initiator/p':
        initiator = ITI39Initiator(None, None, params, destination_url,
                                   destination_oid, test_user_qualification)

    response = asyncio.run(initiator.send_request())
    return response


def action_selector(endpoint_type):
    if endpoint_type == "/iti55initiator":
        return "urn:hl7-org:v3:PRPA_IN201305UV02:CrossGatewayPatientDiscovery"
    elif endpoint_type == "/iti55responder":
        return "urn:hl7-org:v3:PRPA_IN201306UV02:CrossGatewayPatientDiscovery"
    elif endpoint_type == "/iti38initiator":
        return "urn:ihe:iti:2007:CrossGatewayQuery"
    elif endpoint_type == "/iti38responder":
        return "urn:ihe:iti:2007:CrossGatewayQueryResponse"
    elif endpoint_type == "/iti39initiator":
        return "urn:ihe:iti:2007:CrossGatewayRetrieve"
    elif endpoint_type == "/iti39responder":
        return "urn:ihe:iti:2007:CrossGatewayRetrieveResponse"


def create_envelope_with_only_header(relates_to, action=''):
    '''
    Creates the envelope etree object for a response or request object with no Body, only Header.
    Includes SAML assertions for requests only.
    '''

    # Define namespaces
    namespaces = {
        's': 'http://www.w3.org/2003/05/soap-envelope',
        'a': 'http://www.w3.org/2005/08/addressing',
        'query': 'urn:oasis:names:tc:ebxml-regrep:xsd:query:3.0'
    }

    # Create SOAP envelope with namespaces
    soap_env = etree.Element('{{{}}}Envelope'.format(namespaces['s']), nsmap=namespaces)

    # Create SOAP header
    soap_header = etree.SubElement(soap_env, '{{{}}}Header'.format(namespaces['s']))

    header_elements = {
        'Action': {'text': action, 'mustUnderstand': '1'},
        'RelatesTo': {'text': relates_to, 'mustUnderstand': None}
    }

    for element_name, data in header_elements.items():
        element = etree.SubElement(soap_header, '{{{}}}{}'.format(namespaces['a'], element_name))
        element.text = data['text']
        if data['mustUnderstand']:
            element.set('{{{}}}mustUnderstand'.format(namespaces['s']), data['mustUnderstand'])

    return soap_env


def get_relates_to(request):
    '''
    returns the relates_to text for the response
    '''
    relates_to = request.find('.//{*}MessageID')
    if relates_to is not None:
        return relates_to.text
    else:
        return None


def manual_initiator_workflow(event, https_response):
    endpoint_type = event['path']
    request_info = event['body']

    destination_url = request_info['destination_url']
    destination_oid = request_info['destination_oid']
    params = request_info['params']

    response_str = make_and_initiate_request(
        endpoint_type, destination_url, destination_oid, params)
    https_response['body'] = response_str
    return https_response


def lambda_handler(event, context):
    https_response = {
        "statusCode": 200,
        'statusDescription': '200 OK',
        "headers": {'Content-Type': 'application/soap+xml'},
        "isBase64Encoded": False,  # TODO: probably should be false
        "body": ''
    }
    # print("event,", event)

    # expected summaries lambda auto-trigger flow.
    # FE has an API gateway websocket with Summaries lambda, but summaries lambda is calling this lambda with https
    if 'body' in event:
        if 'isBase64Encoded' in event and event['isBase64Encoded']:
            event['body'] = base64.b64decode(event['body'])

        for i in range(2):
            if type(event['body']) is str or type(event['body']) is bytes:
                event['body'] = json.loads(event['body'])
        # print("this should be a json event body", event['body'])
        body = event['body']

        if "action" in body and event['body']["action"] == "getCarequalityPatient":
            try:
                print("getCarequalityPatient")
                connection_id = event['body']['connection_id']

                # get user qualifications
                user_jwt = event['body']['params']['token']
                # user_qualifications is actually the jwt token. need to retrieve the user qualifications from the user_auth lambda
                user_qualifications = utils.get_user_qualifications(user_jwt)
                user_id = user_qualifications['user_id']
                org_db_name = utils.get_org_db_name_from_user_id(ENV, user_id)
                patient_metadatas = event['body']['patient_metadata_set'][
                    : PATIENT_METADATA_SET_LIMIT]  # do not take too many
                asyncio.run(utils.log_metrics_fire_and_forget(
                    user_id,
                    connection_id,
                    {"patient_metadata": patient_metadatas}))
                asyncio.run(utils.log_metrics_fire_and_forget(
                    user_id,
                    connection_id,
                    {"user_info": user_qualifications}))
                print("got user quals", user_qualifications)

                # national umbrella search with stu3 lambda
                national_endpoints = get_national_endpoints()
                # get radius/state-based endpoints for regional
                all_zips = list(set([(patient_metadata["postal_code"], patient_metadata["state"])
                                     for patient_metadata in patient_metadatas]))
                asyncio.run(utils.log_metrics_fire_and_forget(
                    user_id,
                    connection_id, {"zips_found": all_zips}))

                # but first return set of zips + longlat to FE for display
                coords = get_long_lat_from_zips(all_zips)
                asyncio.run(utils.log_metrics_fire_and_forget(
                    user_id,
                    connection_id,
                    {"displayed_coords": coords}))
                coords_return = {"connection_id": connection_id,
                                 "coords": coords,
                                 "message_type": "patient_coords"}
                queue.send_message(MessageBody=json.dumps(coords_return))

                responders = get_endpoints_with_zips(
                    all_zips,
                    each=max(
                        1,
                        MAX_PARALLEL_REQUESTS * ROUNDS_OF_REQUESTS
                        # not using len(all_zips) because a person may have 2 of the same zips and 1 other zip
                        // len(patient_metadatas)
                        - len(national_endpoints))
                )  # dict of zips to list of endpoints

                print("got responders, examples are", [val[:2] for val in responders.values()])
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        connection_id,
                        {"regional_responders": responders}
                    )
                )

                radius_search = CQSearch(responders=responders,
                                         patient_metadatas=patient_metadatas,
                                         user_qualifications=user_qualifications,
                                         org_db_name=org_db_name,
                                         national_endpoints=national_endpoints)

                # ITI 55 regional
                iti55_all_results = radius_search.collect_all_possible_patients()
                iti55latencies = radius_search.collect_55_latencies()
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        connection_id,
                        {"responders_count": len(iti55_all_results)}
                    )
                )
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        connection_id,
                        {"iti55latencies": iti55latencies}
                    )
                )

                radius_search.conflict_checker_dedup()
                iti55_found_pipelines_regional, iti55_success_counter = radius_search.pipelines_with_patient_found()
                print("found at these pipelines, ", iti55_found_pipelines_regional)
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        connection_id,
                        {"regional_found": iti55_found_pipelines_regional}
                    )
                )
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        connection_id,
                        {"iti55_success_counter": iti55_success_counter}
                    )
                )

                if len(iti55_found_pipelines_regional) == 0:  # early termination because no patients are found
                    nf_return = {"connection_id": connection_id,
                                 "message_type": "patient_not_found"}
                    queue.send_message(MessageBody=json.dumps(nf_return))
                    asyncio.run(
                        utils.log_metrics_fire_and_forget(
                            user_id,
                            connection_id,
                            {"status": "not found in national or regional"}
                        )
                    )
                else:
                    found_return = {"connection_id": connection_id,
                                    "pipelines": iti55_found_pipelines_regional,
                                    "message_type": "patient_found"}
                    queue.send_message(MessageBody=json.dumps(found_return))

                    # continue onto docs
                    # make sure both of the national and the regional CQSearch have the same pid
                    # shared_pid = str(uuid.uuid4())
                    # radius_search.internal_additions_v1['pid'] = shared_pid

                    regional_inserted_materials_v1 = radius_search.find_docs_for_conflict_free_patients()
                    asyncio.run(
                        utils.log_metrics_fire_and_forget(
                            user_id,
                            connection_id,
                            {"status": "regional documents inserted"}
                        )
                    )

                    # log 38 39 latencies
                    iti38latencies, iti39latencies = radius_search.collect_38_39_latencies()
                    asyncio.run(
                        utils.log_metrics_fire_and_forget(
                            user_id,
                            connection_id,
                            {"iti38latencies": iti38latencies}
                        )
                    )
                    asyncio.run(
                        utils.log_metrics_fire_and_forget(
                            user_id,
                            connection_id,
                            {"iti39latencies": iti39latencies}
                        )
                    )
                    iti3839_success_counter = radius_search.counter_found_docs()
                    asyncio.run(
                        utils.log_metrics_fire_and_forget(
                            user_id,
                            connection_id,
                            {"iti3839_success_counter": iti3839_success_counter}
                        )
                    )

                    lambda_client = boto3.client('lambda')
                    try:
                        # DB V1 START
                        message_for_summarization_v1 = {
                            "connection_id": connection_id,
                            "note_ids": regional_inserted_materials_v1["doc_ids"],
                            "pid": regional_inserted_materials_v1["pid"],
                            "user_id": user_id,
                            "deprecated": False,
                        }
                        print("this many docs inserted for db v1", len(
                            regional_inserted_materials_v1["doc_ids"]))
                        v1_response = lambda_client.invoke(
                            FunctionName=f"",
                            InvocationType='Event',
                            Payload=json.dumps({"body": message_for_summarization_v1})
                        )
                        asyncio.run(
                            utils.log_metrics_fire_and_forget(
                                user_id,
                                connection_id,
                                {"pid": regional_inserted_materials_v1["pid"]}
                            )
                        )
                        # DB V1 END
                    except Exception:
                        print(traceback.format_exc().replace('\n', '\r'))
                        print("pass v1 write")
                    # the va lambda will sqs communicate with the summaries lambda
                    # and relay the connectionID and userID

                return https_response
            except Exception as e:
                print("exception in summaries lambda auto trigger flow",
                      traceback.format_exc().replace('\n', '\r'))
                error_for_fe = {"connection_id": connection_id,
                                "error": "Error while getting the notes from Carequality.",
                                "timestamp": str(datetime.now()),
                                "message_type": "hie_patient_error"}
                queue.send_message(MessageBody=json.dumps(error_for_fe))
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        connection_id,
                        {"error": traceback.format_exc().replace('\n', '\r')}
                    )
                )
        elif "api" in body and body["api"] == "search":
            conversation_id = body["conversation_id"]
            user_id = body["user_id"]

            # only take 5 even if they pass more
            patient_metadatas = body["patient_metadata_set"][:PATIENT_METADATA_SET_LIMIT]
            # requested output types
            output_categories = body["output_categories"]
            # any custom timing requirements, forward compatible
            other_requirements = body["other_requirements"]

            user_qualifications = utils.get_user_qualifications_tokenless(user_id)
            org_db_name = utils.get_org_db_name_from_user_id(ENV, user_id)
            print("api user_qualifications", user_qualifications)

            asyncio.run(utils.log_metrics_fire_and_forget(
                user_id,
                conversation_id,
                {"patient_metadata": patient_metadatas}))
            asyncio.run(utils.log_metrics_fire_and_forget(
                user_id,
                conversation_id,
                {"user_info": user_qualifications}))

            # national umbrella search with stu3 lambda
            national_endpoints = get_national_endpoints()
            # get radius/state-based endpoints for regional
            all_zips = list(set([(patient_metadata["postal_code"], patient_metadata["state"])
                                 for patient_metadata in patient_metadatas]))
            asyncio.run(utils.log_metrics_fire_and_forget(
                user_id,
                conversation_id, {"zips_found": all_zips}))

            # skip getting longlat because that's for UI only

            responders = get_endpoints_with_zips(
                all_zips,
                each=max(
                    1,
                    MAX_PARALLEL_REQUESTS * ROUNDS_OF_REQUESTS
                    # not using len(all_zips) because a person may have 2 of the same zips and 1 other zip
                    // len(patient_metadatas)
                    - len(national_endpoints))
            )  # dict of zips to list of endpoints

            print("got responders, examples are", [val[:2] for val in responders.values()])
            asyncio.run(
                utils.log_metrics_fire_and_forget(
                    user_id,
                    conversation_id,
                    {"regional_responders": responders}
                )
            )

            radius_search = CQSearch(responders=responders,
                                     patient_metadatas=patient_metadatas,
                                     user_qualifications=user_qualifications,
                                     org_db_name=org_db_name,
                                     national_endpoints=national_endpoints,
                                     for_api=True)

            # ITI 55 regional
            iti55_all_results = radius_search.collect_all_possible_patients()
            iti55latencies = radius_search.collect_55_latencies()
            asyncio.run(
                utils.log_metrics_fire_and_forget(
                    user_id,
                    conversation_id,
                    {"responders_count": len(iti55_all_results)}
                )
            )
            asyncio.run(
                utils.log_metrics_fire_and_forget(
                    user_id,
                    conversation_id,
                    {"iti55latencies": iti55latencies}
                )
            )

            radius_search.conflict_checker_dedup()
            iti55_found_pipelines_regional, iti55_success_counter = radius_search.pipelines_with_patient_found()
            print("found at these pipelines, ", iti55_found_pipelines_regional)
            asyncio.run(
                utils.log_metrics_fire_and_forget(
                    user_id,
                    conversation_id,
                    {"regional_found": iti55_found_pipelines_regional}
                )
            )
            asyncio.run(
                utils.log_metrics_fire_and_forget(
                    user_id,
                    conversation_id,
                    {"iti55_success_counter": iti55_success_counter}
                )
            )

            if len(iti55_found_pipelines_regional) == 0:  # early termination because no patients are found
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        conversation_id,
                        {"status": "not found in national or regional"}
                    )
                )
            else:
                # use the api write
                _ = radius_search.find_docs_for_conflict_free_patients(
                    conversation_id=conversation_id)
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        conversation_id,
                        {"status": "regional documents inserted"}
                    )
                )

                # log 38 39 latencies
                iti38latencies, iti39latencies = radius_search.collect_38_39_latencies()
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        conversation_id,
                        {"iti38latencies": iti38latencies}
                    )
                )
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        conversation_id,
                        {"iti39latencies": iti39latencies}
                    )
                )
                iti3839_success_counter = radius_search.counter_found_docs()
                asyncio.run(
                    utils.log_metrics_fire_and_forget(
                        user_id,
                        conversation_id,
                        {"iti3839_success_counter": iti3839_success_counter}
                    )
                )

        # manual initiator workflow
        elif event['headers']['content-type'] == 'application/json':
            return manual_initiator_workflow(event, https_response)

    return https_response
