import boto3
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from uuid import uuid4

import aiohttp
import requests

ENV = os.environ.get("ENV")
METRICS_LAMBDA_URL = ""
USER_AUTH_LAMBDA = ""


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
            "source": ""
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


def get_org_db_name_from_user_id(env: str, user_id: str):
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


def get_user_qualifications(user_jwt):
    body = json.dumps({"action": "", "token": user_jwt, "otherInfo": {}})
    # TODO: change to boto3
    response = requests.post(USER_AUTH_LAMBDA, data=body)
    if not response.ok:
        raise Exception("could not retrieve user qualifications")
    # response body will be a stringified json of a dict
    user_qualifications = json.loads(response.text)['user_qualifications']
    return user_qualifications


def get_user_qualifications_tokenless(user_id):
    body = json.dumps({"action": "", "otherInfo": {"user_id": user_id}})
    response = requests.post(USER_AUTH_LAMBDA, data=body)
    # TODO: change to boto3
    if not response.ok:
        raise Exception("could not retrieve user qualifications")
    # response body will be a stringified json of a dict
    user_qualifications = json.loads(response.text)['user_qualifications']
    return user_qualifications


def clean_string_for_postgres(s):
    # Remove problematic Unicode sequences (\uXXXX)
    s = re.sub(r'\\u[0-9a-fA-F]{1,4}', '', s)
    # Remove raw byte sequences (\xXX)
    s = re.sub(r'\\x[0-9a-fA-F]{2}', '', s)
    # Remove all non-printable characters
    s = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', s)
    # Escape backslashes and single quotes
    s = s.replace("\\", "\\\\").replace("'", "''")
    return s


def recursive_clean_for_postgres(obj):
    if isinstance(obj, str):
        return clean_string_for_postgres(obj)
    elif isinstance(obj, dict):
        return {key: recursive_clean_for_postgres(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [recursive_clean_for_postgres(element) for element in obj]
    else:
        return obj


def clean_json_for_postgres(json_data):
    cleaned_json = recursive_clean_for_postgres(json_data)
    return json.dumps(cleaned_json)
