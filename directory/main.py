import base64
import boto3
import json
import os
import random
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

with open('national.json') as f:
    NATIONALS = json.load(f)
with open('states.json') as f:
    STATES = json.load(f)

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


def get_endpoints(zip_states, each=1, exclude_national=True):
    '''
    get endpoints around each of the zip code,
    return as close to `each` endpoints as possible but do not exceed.
    prioritize 10 mi radius, then 30, then 100
    '''
    print("received each", each)
    zip_states = [[zipcode.split('-')[0], state]
                  if '-' in zipcode
                  else [zipcode, state]
                  for zipcode, state in zip_states]
    excluded = set()
    if exclude_national:
        for endpoint in NATIONALS:
            excluded.add((endpoint['oid'], endpoint['iti55_responder']))

    # TODO: remove
    temp = []
    for zipcode, state in zip_states:
        temp.append([zipcode.lstrip("0"), state])

    zip_states = temp

    print("processed zip codes & states,", zip_states)
    # get the nearby zipcodes
    connection = get_cq_db_connection()
    cur = connection.cursor()

    zips_to_endpoints = {}
    radius_priority_list = [10, 30, 100]

    for zipcode, state in zip_states:
        # first, grab all state endpoints
        state_endpoints = STATES.get(state, [])
        filled_endpoints = [(endpoint['oid'],
                             endpoint['name'],
                             endpoint['iti55_responder'],
                             endpoint['iti38_responder'],
                             endpoint['iti39_responder'])
                            for endpoint in state_endpoints]
        # then fill by radius 10 -> 100
        for radius in radius_priority_list:
            radius_column = 'neighboring_zipcodes_' + str(radius) + 'mi'
            cur.execute("SELECT " + radius_column +
                        " FROM zipcode_neighbors WHERE zipcode = %s", (zipcode,))
            nearby_zipcodes = cur.fetchall() or []
            set_nearby_zipcodes = set()
            for zip_list_tup in nearby_zipcodes:
                new_zips = zip_list_tup[0]
                for new_zip in new_zips:
                    set_nearby_zipcodes.add(new_zip)
            nearby_zipcodes = set_nearby_zipcodes
            # TODO: remove
            nearby_zipcodes = tuple([zipcode.rjust(5, "0") for zipcode in nearby_zipcodes])
            print("nearby_zipcodes,", nearby_zipcodes)

            if nearby_zipcodes:
                # get the endpoints
                cur.execute(
                    """
                    SELECT oid, name, iti55_responder, iti38_responder, iti39_responder
                    FROM prod_stu3_directory
                    WHERE zipcode IN %s
                    AND managing_org NOT IN %s
                    AND status
                    """,
                    (nearby_zipcodes, tuple(BAD_IMPLEMENTERS))
                )
                endpoints = cur.fetchall()
                if len(filled_endpoints) + len(endpoints) >= each:  # fill "up to"
                    filled_endpoints += random.choices(endpoints, k=each - len(filled_endpoints))
                    break

        endpoint_dicts = utils.validate_endpoint_dicts(filled_endpoints, exclude=excluded)

        # TODO: remove
        zipcode = zipcode.rjust(5, "0")
        zips_to_endpoints[zipcode] = endpoint_dicts

    print({zipcode: len(zips_to_endpoints[zipcode]) for zipcode in zips_to_endpoints})

    cur.close()
    connection.close()

    response['body'] = json.dumps(zips_to_endpoints)
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
    if action == 'download_data':
        print('downloading data...')
        success = download_data()
        if success:
            act = {"body": {"action": "process_data"}}

            lambda_client = boto3.client('lambda')
            lambda_client.invoke(FunctionName=context.function_name,
                                 InvocationType='Event',
                                 Payload=json.dumps(act))

            # act = json.dumps(act)
        else:
            print("failed to download data")
        return

    elif action == 'process_data':
        print('processing data...')
        final_list = process_data()
        s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=f'processed_data.json',
                             Body=json.dumps(final_list))

        batch_size = 30000
        for i in range(0, len(final_list), batch_size):
            print("called insert i=", i)
            act = {"body": {"action": "insert_data", "start": i, "batch_size": batch_size}}

            lambda_client = boto3.client('lambda')
            lambda_client.invoke(FunctionName=context.function_name,
                                 InvocationType='Event',
                                 Payload=json.dumps(act))
            time.sleep(60)
        return
    elif action == 'insert_data':
        print('inserting data...')
        final_list = utils.read_data_from_s3('processed_data.json', s3_client, S3_BUCKET_NAME)
        start = event['body']['start']
        batch_size = event['body']['batch_size']
        insert_table(final_list, start, batch_size)

    elif action == 'getNationalEndpoints':
        response['body'] = json.dumps(NATIONALS)
        return response
    elif action == 'getEndpoints':
        print("getting endpoints...")
        params = event['body']['params']
        zip_codes = params['zip_codes']  # [[zip1, state1], [zip2, state2], ...]
        each = params['each'] if 'each' in params else False
        return get_endpoints(zip_codes, each)
    elif action == 'augmentLongLat':
        return insert_long_lat()
