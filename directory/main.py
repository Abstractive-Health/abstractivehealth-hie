import base64
import boto3
import json
import os
import time

import requests
from new_insert import *

import utils

ENV = os.environ.get("ENV")
secretsmanager = boto3.client('secretsmanager')
secret_id = ""
secret_params = {}
s3_client = boto3.client('s3', endpoint_url="https://s3.amazonaws.com/")

# host used to connect to PostgreSQL
DB_HOST_NAME = ''
S3_BUCKET_NAME = ''
CQPROD_STU3_TABLE_NAME = ''

response = {
    "statusCode": 200,
    "statusDescription": "200 OK",
    "isBase64Encoded": False,
    "headers": {
        "Content-Type": "application/json; charset=utf-8",
        "Access-Control-Allow-Origin": '*'
    },
    # will be overwritten if there's an error
    "body": json.dumps({'success': 'success'})
}


def get_coordinates(zip_code):
    base_url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": zip_code,
        "format": "json",
        "limit": 1,
    }

    response = requests.get(base_url, params=params)
    data = response.json()

    if data:
        location = data[0]
        latitude = location.get('lat')
        longitude = location.get('lon')
        return latitude, longitude
    else:
        return None, None


def insert_long_lat():
    '''
    for each zipcode in the zipcode_neighbors table, insert longitude and latitude
    '''
    conn = get_cq_db_connection()
    cur = conn.cursor()

    cur.execute(
        'SELECT zipcode FROM zipcode_neighbors WHERE longitude is NULL AND latitude is NULL ORDER BY zipcode DESC')
    zipcodes = cur.fetchall() or []
    zipcodes = [zipcode[0] for zipcode in zipcodes]

    print(len(zipcodes))
    # wrap the above loop in tqdm
    for i in range(len(zipcodes)):
        zipcode = zipcodes[i]
        zipcode = zipcode.rjust(5, '0')
        if i % 1000 == 0:
            print("at", i)
        try:
            latitude, longitude = get_coordinates(zipcode)
            if longitude and latitude:
                cur.execute(
                    'UPDATE zipcode_neighbors SET latitude = %s, longitude = %s WHERE zipcode = %s',
                    (latitude, longitude, zipcode))
                conn.commit()
            time.sleep(0.2)
        except Exception as e:
            time.sleep(1)
            print("exception at ", i)
            print(f"Error processing {zipcode}: {e}")
            continue
    return


def get_endpoints(zip_codes, radius=100, exclude=[]):
    '''
    get endpoints of radius around any of the zip codes
    output should not contain duplicates or invalid endpoints (bad urls)
    if exclude is not empty, then exclude those endpoints according to name.
    '''
    zip_codes = [zip_code.split('-')[0] if '-' in zip_code else zip_code for zip_code in zip_codes]

    # TODO: remove
    temp = []
    for zipcode in zip_codes:
        temp.append(zipcode.lstrip("0"))

    zip_codes = temp

    print("processed zip codes,", zip_codes)
    # get the nearby zipcodes
    connection = get_cq_db_connection()
    cur = connection.cursor()

    radius_column = 'neighboring_zipcodes_' + str(radius) + 'mi'
    cur.execute("SELECT " + radius_column +
                " FROM zipcode_neighbors WHERE zipcode IN %s", (tuple(zip_codes),))
    nearby_zipcodes = cur.fetchall() or []
    print("nearby_zipcodes,", nearby_zipcodes)
    set_nearby_zipcodes = set()
    for zip_list_tup in nearby_zipcodes:
        new_zips = zip_list_tup[0]
        for new_zip in new_zips:
            set_nearby_zipcodes.add(new_zip)
    nearby_zipcodes = set_nearby_zipcodes
    # TODO: remove
    nearby_zipcodes = tuple([zipcode.rjust(5, "0") for zipcode in nearby_zipcodes])

    # get the endpoints
    cur.execute(
        "SELECT oid, name, iti55_responder, iti38_responder, iti39_responder FROM %s WHERE zipcode IN %s and status",
        (CQPROD_STU3_TABLE_NAME, nearby_zipcodes,))
    endpoints = cur.fetchall()

    cur.close()
    connection.close()
    # post process oid
    endpoint_dicts = set()
    for endpoint in endpoints:
        oid = endpoint[0] if 'urn:oid:' not in endpoint[0] else endpoint[0].split('urn:oid:')[1]

        validated_endpoint = utils.validate_endpoint_dict({
            'oid': oid,
            'name': endpoint[1],
            'iti55_responder': endpoint[2],
            'iti38_responder': endpoint[3],
            'iti39_responder': endpoint[4]
        },
            set(exclude),
        )

        if validated_endpoint is not None:
            endpoint_dicts.add(str(validated_endpoint))

    # here's how you would constrain to integrated pipelines, though epic makes it hard
    # SELECT *
    # FROM stu3_directory
    # WHERE resource->'Organization'->'id'->>'value' IN ('2.16.840.1.113883.3.564.1', 'urn:oid:2.16.840.1.113883.3.564.1')
    # OR resource->'Organization'->'partOf'->'identifier'->'value'->>'value' IN ('2.16.840.1.113883.3.564.1', 'urn:oid:2.16.840.1.113883.3.564.1');

    response['body'] = json.dumps([eval(endpoint) for endpoint in endpoint_dicts])
    print("about to return response", response['body'])
    return response


def lambda_handler(event, context):
    """
    Starts the lambda process in AWS.
    Returns an HTTP response.
    Arguments:
    event -- the data that is passed into the lambda at runtime
    """
    print("event:", event)
    if 'isBase64Encoded' in event and event['isBase64Encoded']:
        event['body'] = base64.b64decode(event['body'])
    for i in range(2):
        if type(event['body']) is str or type(event['body']) is bytes:
            event['body'] = json.loads(event['body'])

    action = event['body']['action']
    if action == 'insert_downloaded_directory':
        pass
        # return insert_downloaded_directory()
    elif action == 'insert_prod_directory':
        return insert_prod_directory()
    elif action == 'getNationalEndpoints':
        with open('national.json') as f:
            response['body'] = json.dumps(json.load(f))
        return response
    elif action == 'getEndpoints':
        print("getting endpoints...")
        params = event['body']['params']
        radius = params['radius'] if 'radius' in params else 100
        country = params['country'] if 'country' in params else "US"
        # these national endpoints we got responses for already
        exclude = params['exclude'] if 'exclude' in params else []

        if country not in ["US", "USA"]:
            return []
        zip_codes = params['zip_codes']
        return get_endpoints(zip_codes, radius, exclude)
    elif action == 'augmentLongLat':
        return insert_long_lat()
