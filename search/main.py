import asyncio
import base64
import boto3
import json
import os
import traceback
import uuid

import psycopg2
import requests
from chained import *
from iti38initiator import ITI38Initiator
from iti38responder import ITI38Responder
from iti39initiator import ITI39Initiator
from iti39responder import ITI39Responder
from iti55initiator import ITI55Initiator
from iti55responder import ITI55Responder
from lxml import etree

import utils

ENV = os.environ.get("ENV")
secretsmanager = boto3.client('secretsmanager')
secret_id = ""
secret_params = {}

STU3_DIRECTORY_LAMBDA = ""

def get_db_connection(database=''):
    return psycopg2.connect(
        host='', port=,
        user=secret_params['db_username'],
        password=secret_params['db_password'],
        database=database)


def get_endpoints_with_zips(zip_codes, state, radius=10, country="US", exclude=[]):
    '''
    calls stu3 directory to get active endpoints within radius of zip code
    '''
    body = json.dumps({
        "action": "getEndpoints",
        "params": {
            "radius": radius,
            "state": state,
            "zip_codes": zip_codes,
            "country": country,
            "exclude": exclude
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


def make_and_react_to_xml(endpoint_type, xml_message, cur):
    '''
    makes and pings endpoint back with appropriate action if we're pinged by someone else
    '''
    if endpoint_type == "/iti55initiator" or endpoint_type == "/iti38initiator" or endpoint_type == "/iti39initiator":
        response_unwrapped_body = '<?xml version="1.0" encoding="UTF-8"?><Response><Message>reached our domain but did not specify any responder, so your request is not processed. please select an endpoint</Message></Response>'
        # something like ack; not required. what's important is processing the response into our db
        return response_unwrapped_body

    elif endpoint_type == "/iti55responder":
        print("iti55responder pinged")
        responder = ITI55Responder(cur, xml_message)
        # body as an element without being wrapped in <Body> tags
        response_unwrapped_body = responder.generate_response_body()
        return response_unwrapped_body

    elif endpoint_type == "/iti38responder":
        print("iti38responder pinged")
        responder = ITI38Responder(cur, xml_message)
        response_unwrapped_body = responder.generate_response_body()
        return response_unwrapped_body

    elif endpoint_type == "/iti39responder":
        print("iti39responder pinged")
        responder = ITI39Responder(cur, xml_message)
        response_unwrapped_body = responder.generate_response_body()
        return response_unwrapped_body

    else:
        print("path incorrect")
        response_unwrapped_body = '<?xml version="1.0" encoding="UTF-8"?><Response><Message>reached our domain but did not specify any endpoint, so your request is not processed. please select an endpoint</Message></Response>'
        return response_unwrapped_body


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



def responder_workflow(event, https_response):
    db_connection = get_db_connection()
    cur = db_connection.cursor()

    xml_message = event['body']

    # the entire body might be be base64 encoded coming in
    if 'isBase64Encoded' in event and event['isBase64Encoded']:
        xml_message = base64.b64decode(xml_message)

    endpoint_type = event['path']
    try:
        tree = etree.fromstring(xml_message)
    except:
        envelope = utils.extract_envelope_content(xml_message)
        tree = etree.fromstring(envelope)
    unwrapped_body = make_and_react_to_xml(endpoint_type, tree, cur)

    relates_to = get_relates_to(tree)  # should be None if we are initiator
    action = action_selector(endpoint_type)

    envelope = create_envelope_with_only_header(relates_to, action)
    body = etree.SubElement(envelope, '{{{}}}Body'.format(envelope.nsmap['s']))
    body.append(unwrapped_body)

    endpoint_response = etree.tostring(envelope, pretty_print=True, encoding="UTF-8")

    https_response['body'] = endpoint_response
    print("want to return the following http_response,", https_response)
    db_connection.commit()
    db_connection.close()

    return https_response


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
        "isBase64Encoded": False, 
        "body": ''
    }
    print("event,", event)

    # expected responder workflow
    if 'headers' in event and 'content-type' in event['headers'] and 'xml' in event['headers'][
            'content-type']:
        return responder_workflow(event, https_response)

    # parallel pipeline
    elif 'body' in event:
        if 'isBase64Encoded' in event and event['isBase64Encoded']:
            event['body'] = base64.b64decode(event['body'])

        for i in range(2):
            if type(event['body']) is str or type(event['body']) is bytes:
                event['body'] = json.loads(event['body'])
        print("this should be a json event body", event['body'])

        if "action" in event['body'] and event['body']["action"] == "getCarequalityPatient":
            try:
                print("getCarequalityPatient")
                connection_id = event['body']['connection_id']
                non_metadata_keys = set(["token", "location_search_zip", "location_search_state"])
                patient_metadata = {
                    key: value for key, value in event['body']['params'].items()
                    if key not in non_metadata_keys
                }
                
                user_qualifications = {}
                user_id = user_qualifications['user_id']

                # national umbrella search with stu3 lambda
                national_endpoints = get_national_endpoints()
                national_search = CQSearch(responders=national_endpoints,
                                           patient_metadata=patient_metadata,
                                           user_qualifications=user_qualifications,
                                           national=True)
                national_search.collect_all_possible_patients()
                # patient past zips according to the national endpoints
                past_zips = national_search.conflict_checker()

                # zip-based location search 
                # recent change: location_search_zip is becoming a NON-EMPTY list
                zip_codes = list(set(event['body']['params']['location_search_zip'] + past_zips))
                print("zips after national search", zip_codes)

                iti55_found_pipelines_national = national_search.pipelines_with_patient_found()

                # continue. these 2 params are not used for now
                state = event['body']['params']['location_search_state'] if 'location_search_state' in event['body'][
                    'params'] else "NY"
                country = event['body']['params']['country'] if 'country' in event['body'][
                    'params'] else "US"

                radius_priority_list = [10, 30, 100]

                responders = get_endpoints_with_zips(
                    zip_codes, state, radius=radius_priority_list.pop(), country=country)
                while len(responders) > 80 and len(radius_priority_list) > 0:
                    radius = radius_priority_list.pop()
                    responders = get_endpoints_with_zips(zip_codes,
                                                         state,
                                                         radius=radius,
                                                         country=country,
                                                         exclude=iti55_found_pipelines_national)

                print("got " + str(len(responders)) + " responders, starting with", responders[:5])

                radius_search = CQSearch(responders=responders[:200],
                                         patient_metadata=patient_metadata,  # can handle at most 200 at a time
                                         user_qualifications=user_qualifications)

                # ITI 55 regional
                radius_search.collect_all_possible_patients()
                radius_search.conflict_checker()
                iti55_found_pipelines_regional = radius_search.pipelines_with_patient_found()
                iti55_return = iti55_found_pipelines_national + iti55_found_pipelines_regional

                if len(iti55_return) == 0:  # early termination because no patients are found
                    nf_return = {"connection_id": connection_id,
                                 "message_type": "patient_not_found"}

                else:
                    found_return = {"connection_id": connection_id,
                                    "pipelines": iti55_return, "message_type": "patient_found"}

                    # continue onto docs
                    # make sure both of the national and the regional CQSearch have the same pid
                    shared_pid = str(uuid.uuid4())
                    radius_search.internal_additions['pid'] = shared_pid
                    national_search.internal_additions['pid'] = shared_pid


                    # to avoid race condition, these have to be done in two steps;
                    # TODO: figure out how to merge 2 CQSearch Objects
                    # this will speed up document queries by 2x
                    regional_inserted_materials = radius_search.find_docs_for_conflict_free_patients()
                    national_inserted_materials = national_search.find_docs_for_conflict_free_patients()

                    # search is done.


                return https_response
            except Exception as e:
                print(traceback.format_exc().replace('\n', '\r'))

        # manual initiator workflow
        elif event['headers']['content-type'] == 'application/json':
            return manual_initiator_workflow(event, https_response)

    return https_response
