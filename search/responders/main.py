import asyncio
import base64
import boto3
import json
import os
import traceback
import uuid

import psycopg2
import requests
from iti38responder import ITI38Responder
from iti39responder import ITI39Responder
from iti55responder import ITI55Responder
from lxml import etree

import utils

ENV = os.environ.get("ENV")
secretsmanager = boto3.client('secretsmanager')
secret_id = f"{ENV}-lambda-hie-patient"
secret_params = json.loads(secretsmanager.get_secret_value(SecretId=secret_id)["SecretString"])
GOOGLE_API_KEY = secret_params['google_geo_api_key']

dynamodb = boto3.resource('dynamodb').Table(f"")
sqs = boto3.resource('sqs')
queue = sqs.Queue(f'')
STU3_DIRECTORY_LAMBDA = f''''''
USER_AUTH_LAMBDA = ""


def get_db_connection(database=''):
    return psycopg2.connect(
        host=f'', port=5432,
        user=secret_params['db_username'],
        password=secret_params['db_password'],
        database=database)

def make_and_react_to_xml(endpoint_type, xml_message):
    '''
    makes and pings endpoint back with appropriate action if we're pinged by someone else
    '''
    if endpoint_type == "/iti55initiator" or endpoint_type == "/iti38initiator" or endpoint_type == "/iti39initiator":
        response_unwrapped_body = '<?xml version="1.0" encoding="UTF-8"?><Response><Message>reached our domain but did not specify any responder, so your request is not processed. please select an endpoint</Message></Response>'
        # something like ack; not required. what's important is processing the response into our db
        return response_unwrapped_body

    elif endpoint_type == "/iti55responder":
        print("iti55responder pinged")
        responder = ITI55Responder(xml_message)
        # body as an element without being wrapped in <Body> tags
        response_unwrapped_body = responder.generate_response_body()
        return response_unwrapped_body

    elif endpoint_type == "/iti38responder":
        print("iti38responder pinged")
        responder = ITI38Responder(xml_message)
        response_unwrapped_body = responder.generate_response_body()
        return response_unwrapped_body

    elif endpoint_type == "/iti39responder":
        print("iti39responder pinged")
        responder = ITI39Responder(xml_message)
        response_unwrapped_body = responder.generate_response_body()
        return response_unwrapped_body

    else:
        print("endpoint not specified")
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


def get_user_qualifications(user_jwt):
    body = json.dumps({"action": "", "token": user_jwt, "otherInfo": {}})
    response = requests.post(USER_AUTH_LAMBDA, data=body)
    if not response.ok:
        raise Exception("could not retrieve user qualifications")
    # response body will be a stringified json of a dict
    user_qualifications = json.loads(response.text)['user_qualifications']
    return user_qualifications


def responder_workflow(event, https_response):
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

    unwrapped_body = make_and_react_to_xml(endpoint_type, tree)

    relates_to = get_relates_to(tree)  # should be None if we are initiator
    action = action_selector(endpoint_type)

    envelope = create_envelope_with_only_header(relates_to, action)
    body = etree.SubElement(envelope, '{{{}}}Body'.format(envelope.nsmap['s']))
    body.append(unwrapped_body)

    endpoint_response = etree.tostring(envelope, pretty_print=True, encoding="UTF-8")

    https_response['body'] = endpoint_response
    print("want to return the following http_response,", https_response)

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

    # expected responder workflow
    if 'headers' in event and 'content-type' in event['headers'] and 'xml' in event['headers'][
            'content-type']:
        return responder_workflow(event, https_response)


    return https_response
