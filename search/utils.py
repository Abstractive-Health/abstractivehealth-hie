import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from uuid import uuid4

import aiohttp
import requests


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

