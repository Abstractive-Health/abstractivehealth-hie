import boto3
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from uuid import uuid4

import aiohttp
import psycopg2
import requests

ENV = os.environ.get("ENV")
secretsmanager = boto3.client('secretsmanager')
secret_id = f"{ENV}-lambda-hie-patient"
secret_params = json.loads(secretsmanager.get_secret_value(SecretId=secret_id)["SecretString"])
METRICS_LAMBDA_URL = ""

GENDER_CODE_DISPLAY_MAP = json.loads(open('template/gender_mapping.json').read())
SPECIALTY_CODE_DISPLAY_MAP = json.loads(open('template/specialty_mapping.json').read())


def get_db_connection(database=''):
    return psycopg2.connect(
        host=f'', port=5432,
        user=secret_params['db_username'],
        password=secret_params['db_password'],
        database=database)


def ambiguous_strip(s):
    '''
    some strings are enclosed in single or double quotes, strip quotes if present
    '''
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    elif s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    elif s.startswith("\'") and s.endswith("\'"):
        return s[2:-2]
    elif s.startswith('\"') and s.endswith('\"'):
        return s[2:-2]
    else:
        return s


def gender_ambiguous_formatting(s):
    if s == 'M' or s == 'Male':
        return 'male'
    elif s == 'F' or s == 'Female':
        return 'female'


def birthdate_ambiguous_formatting(s):
    if len(s) < 8:
        return '0000-00-00'
    if '/' in s:
        return s.replace('/', '-')
    elif len(s) == 8:
        return s[0:4] + '-' + s[4:6] + '-' + s[6:8]
    elif not s[2].isdigit():
        mdy = s.split('-')
        return mdy[2] + '-' + mdy[0] + '-' + mdy[1]
    else:
        return s


def personalize_wsdl(destination_url, responder_iti_no, template):
    if not template:
        template_name = "gazelle_ITI" + responder_iti_no + "_responder.wsdl"
        with open("wsdls/"+template_name, 'r') as f:
            template = f.read()
    if responder_iti_no == '38':
        new_wsdl = template.replace(
            '', destination_url)
    elif responder_iti_no == '39':
        new_wsdl = template.replace(
            '', destination_url)
    elif responder_iti_no == '55':
        new_wsdl = template.replace(
            '', destination_url)
    else:
        raise Exception(f"unknown responder iti no {responder_iti_no}")

    save_name = str(uuid4())
    # write wsdl
    # with open("/tmp/"+save_name, 'w') as f:
    # f.write(new_wsdl)
    #    print("foo")

    return new_wsdl, save_name


def format_prepped_request(prepped, encoding=None):
    # prepped has .method, .path_url, .headers and .body attribute to view the request
    encoding = encoding or requests.utils.get_encoding_from_headers(prepped.headers)
    body = prepped.body.decode(encoding) if encoding else '<binary data>'
    headers = '\n'.join(['{}: {}'.format(*hv) for hv in prepped.headers.items()])
    return f"""{prepped.method} {prepped.path_url} HTTP/1.1{headers}{body}"""


def extract_envelope_content(envelope_bytes):
    # Convert bytes to string
    try:
        envelope_string = envelope_bytes.decode('utf-8')
    except:
        envelope_string = envelope_bytes

    try:
        # Define the regular expression pattern to match the entire envelope
        pattern = re.compile(r'<(?:[^>:]+:)?Envelope[^>]*>.*?</(?:[^>:]+:)?Envelope>', re.DOTALL)

        # Search for the envelope within the provided string
        match = pattern.search(envelope_string)

        if match:
            # Return the entire envelope (including tags and content)
            return match.group(0)
        else:
            return None
    except Exception as e:
        print("Error extracting envelope content:", e)
        print("envelope_string:", envelope_string)
        return None


def json2xml(json_obj, line_padding=""):
    result_list = list()

    json_obj_type = type(json_obj)

    if json_obj_type is list:
        for sub_elem in json_obj:
            result_list.append(json2xml(sub_elem, line_padding))

        return "\n".join(result_list)

    if json_obj_type is dict:
        for tag_name in json_obj:
            sub_obj = json_obj[tag_name]
            result_list.append("%s<%s>" % (line_padding, tag_name))
            result_list.append(json2xml(sub_obj, "\t" + line_padding))
            result_list.append("%s</%s>" % (line_padding, tag_name))

        return "\n".join(result_list)

    return "%s%s" % (line_padding, json_obj)


async def log_metrics_fire_and_forget(user_id=None, conn_or_conv_id=None, json_message=None):
    '''
    shoots a logging request to the metrics lambda
    does not await response, does not care if the logging is successful
    '''
    try:
        timestamp = datetime.now(timezone.utc)
        payload = {
            "user_id": user_id,
            "connection_id": conn_or_conv_id,
            "json_message": json_message,
            "timestamp": timestamp.isoformat(),
            "source": "hie-patient"
        }

        headers = {
            "content-type": "application/json"
        }

        # use aiohttp to fire and forget
        async with aiohttp.ClientSession() as session:
            async with session.post(METRICS_LAMBDA_URL, json=payload, headers=headers) as response:
                pass
    except Exception as e:
        print("Error in log_metrics_fire_and_forget:", e)
    return


def get_org_db_name_from_user_id(user_id: str):
    '''
    returns the name of the organization's DB that the user is associated with
    '''
    lambda_client = boto3.client('lambda')
    user_auth_response = lambda_client.invoke(
        FunctionName=f"",
        InvocationType="RequestResponse",
        Payload=json.dumps({"body": json.dumps({
            "action": "",
            "token": "",
            "otherInfo": {
                "user_id": user_id
            }
        })})
    )
    response_body_str = json.loads(user_auth_response["Payload"].read().decode("utf-8"))["body"]

    try:
        org_db_name = json.loads(response_body_str)["org_db_name"]
        return org_db_name

    except KeyError:
        raise Exception(
            f"redacted")


def is_valid_org_hcid(org_hcid):
    '''
    return True if org_hcid has valid format, False if not
    valid format of org_hcid e.g. .1
    '''
    org_hcid = org_hcid[8:] if org_hcid.startswith("urn:oid:") else org_hcid
    valid_org_hcid_pattern = re.escape('') + r'\.\d+$'
    if re.match(valid_org_hcid_pattern, org_hcid):
        return True
    else:
        print(f"org_hcid {org_hcid} is not in the correct format")
        return False


def get_org_db_name_from_org_id(org_id: str):
    """
    given a user's org_id, gets the name of the database for
    that org.

    a catch here is that databases cannot be named using
    hyphens, so we must replace the '-' in the org_id with
    '_'. we'll need to reverse engineer this later whenever
    referring to this organization's database.
    """

    org_id_underscored = org_id.replace('-', '_')
    return f"app_org_{org_id_underscored}"


def get_org_id_and_name_from_org_hcid(org_hcid: str):
    '''
    returns the name of the organization's DB that the user is associated with
    '''
    if not org_hcid:
        return None

    lambda_client = boto3.client('lambda')
    user_auth_response = lambda_client.invoke(
        FunctionName=f"",
        InvocationType="RequestResponse",
        Payload=json.dumps({"body": json.dumps({
            "action": "",
            "token": "",
            "otherInfo": {
                "org_hcid": org_hcid
            }
        })})
    )
    response_body_str = json.loads(
        user_auth_response["Payload"].read().decode("utf-8"))["body"]

    try:
        response_body = json.loads(response_body_str)
        org_id = response_body["org_id"]
        org_name = response_body["org_name"]
        return org_id, org_name
    except:
        # don't raise an exception so that our pipeline won't break
        print(
            f"redacted")
        return None


def json2xml(json_obj):
    summary = json_obj
    narrative_content = ''
    for key, value in summary.items():
        narrative_content += f'{key}: \n'
        if isinstance(value, list):
            for item in value:
                narrative_content += f'- {item}\n'
        else:  # value is None or string
            narrative_content += str(value) + '\n'

    return narrative_content


def get_patient_metadata(org_id, pid):
    '''
    ping resolver lambda to get patient metadata
    '''
    lambda_client = boto3.client('lambda')
    response = lambda_client.invoke(
        FunctionName=f"",
        InvocationType='RequestResponse',
        Payload=json.dumps({"body": json.dumps({
            "use_case": "",
            "org_id": org_id,
            "pid": pid,
        })})
    )
    response_body_str = json.loads(response["Payload"].read().decode("utf-8"))["body"]
    patient_metadata = json.loads(response_body_str)

    return patient_metadata


def get_provider_metadata(user_id):
    '''
    get provider metadata
    '''
    lambda_client = boto3.client('lambda')
    response = lambda_client.invoke(
        FunctionName=f"",
        InvocationType='RequestResponse',
        Payload=json.dumps({"body": json.dumps({
            "action": "",
            "otherInfo": {
                "user_id": user_id
            },
        })})
    )
    response_body_str = json.loads(response["Payload"].read().decode("utf-8"))["body"]
    provider_metadata = json.loads(response_body_str)

    return provider_metadata


def get_org_metadata(org_id):
    '''
    get org metadata
    '''
    lambda_client = boto3.client('lambda')
    response = lambda_client.invoke(
        FunctionName=f"",
        InvocationType='RequestResponse',
        Payload=json.dumps({"body": json.dumps({
            "action": "",
            "otherInfo": {
                "org_id": org_id,
            },
        })})
    )
    response_body_str = json.loads(response["Payload"].read().decode("utf-8"))["body"]
    org_metadata = json.loads(response_body_str)

    return org_metadata
