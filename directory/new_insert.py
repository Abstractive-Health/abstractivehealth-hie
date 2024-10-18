import boto3
import json
import os

import psycopg2
import psycopg2.extras
import requests

import utils

ENV = os.environ.get("ENV")
secretsmanager = boto3.client('secretsmanager')
secret_id = ""
secret_params = {}
s3_client = boto3.client('s3', endpoint_url="https://s3.amazonaws.com/")

# host used to connect to PostgreSQL
DB_HOST_NAME = ''
S3_BUCKET_NAME = ''
TABLE_NAME = ''

def download_data():
    cq_dir_api_key = secret_params['prod_api_key']
    cq_dir_api_url = secret_params['prod_url']
    # accept encoding: gzip
    headers = {'Accept-Encoding': 'gzip'}

    # start of the range of entries to download
    start = 0
    # number of entries to download in each request
    COUNT = 10000
    # limit of entries to download, should be very large
    LIMIT = 100000
    complete_json = []

    while start < LIMIT:
        params = {'apikey': cq_dir_api_key, "_format": "json", "_count": COUNT, "_start": start}
        try:
            r = requests.get(cq_dir_api_url, headers=headers, params=params)
            if r.status_code != 200:
                print(f'Error, status code {r.status_code}, starting at {start}')
                return False  # early return because cannot trust the pull, so will not write a new json
            response = r.content.decode('utf-8')
            # add the response to the complete_json list
            complete_json.append(response)
            print(start)
            # print(response)
            # response = response.content.decode('utf-8')
        except:
            try:
                r = requests.get(cq_dir_api_url, headers=headers, params=params)
                if r.status_code != 200:
                    print(f'Error, status code {r.status_code}')
                    break
                response = r.content.decode('utf-8')
                # add the response to the complete_json list
                complete_json.append(response)
                print(start)
            except:
                break

        start = start + COUNT

    s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=f'downloaded_json_list.json',
                         Body=json.dumps(complete_json))
    return True


def traverse_json_list(json_list):
    '''
    Traverses the list of jsons pulled and reformats them as a list of dictionaries for each entry.
    Arguments:
    json_list: list of jsons
    Returns:
    final_list: list of dictionaries
    '''
    # PART 1: GENERATE DICTIONARY OF ID TO URLS

    # Defines a function that recursively gets the URLs for a given ID later in the process.
    def recursively_get_urls(id, id_dict):
        if id in id_dict:
            if id_dict[id]['iti55'] is not None and id_dict[id]['iti38'] is not None and id_dict[id]['iti39'] is not None:
                return id_dict[id]
            elif id_dict[id]['part_of'] is not id:
                return recursively_get_urls(id_dict[id]['part_of'], id_dict)
            else:
                return None
        else:
            return None

    oid_dict = {}

    json_list_length = len(json_list)
    for i in range(json_list_length):
        json_obj = json_list[i]['Bundle']['entry']
        json_obj_length = len(json_obj)
        for j in range(json_obj_length):
            sub_json_obj = json_obj[j]['resource']

            oid = None
            iti55 = None
            iti38 = None
            iti39 = None
            partOf = None

            try:
                oid = sub_json_obj['Organization']['id']['value']
            except KeyError:
                pass

            # Try to access the URLs, if they exist, store them in the dictionary, if not set to None.
            try:
                for k in range(len(sub_json_obj['Organization']['contained'])):
                    try:
                        use_name = sub_json_obj['Organization']['contained'][k]['Endpoint'][
                            'name']['value']
                        if use_name == 'Patient Discovery':
                            iti55 = sub_json_obj['Organization']['contained'][k]['Endpoint'][
                                'address']['value']
                        if use_name == 'Query for Documents':
                            iti38 = sub_json_obj['Organization']['contained'][k]['Endpoint'][
                                'address']['value']
                        if use_name == 'Retrieve Documents':
                            iti39 = sub_json_obj['Organization']['contained'][k]['Endpoint'][
                                'address']['value']
                    except KeyError:
                        # will hopefully be inherited from parent later
                        pass
            except KeyError:
                # will hopefully be inherited from parent later
                pass

            try:
                partOf = sub_json_obj['Organization']['partOf']['identifier']['value']['value']
                partOf = partOf.replace('urn:oid:', '')
            except KeyError:
                pass

            oid_dict[oid] = {
                'id': oid,
                'iti55': iti55,
                'iti38': iti38,
                'iti39': iti39,
                'part_of': partOf
            }

    # PART 2: GENERATE LIST OF DICTIONARIES FOR EACH ENTRY, INCLUDING URLS USING ID_DICT

    final_list = []

    def country_code_clean(country_code):
        if len(country_code) > 2:
            return country_code[:2]
        else:
            return country_code

    def state_clean(state_code):
        if len(state_code) > 2:
            return state_code[:2].upper()
        else:
            return state_code.upper()

    def zipcode_clean(zipcode):
        if len(zipcode) == 5:
            return zipcode
        elif len(zipcode) > 5:
            return zipcode[:5]
        elif len(zipcode) < 5:
            return (5-len(zipcode))*'0' + zipcode

    json_list_length = len(json_list)
    for i in range(json_list_length):
        json_obj = json_list[i]['Bundle']['entry']
        json_obj_length = len(json_obj)
        for j in range(json_obj_length):
            sub_json_obj = json_obj[j]['resource']

            oid = None
            name = None
            address = None
            country_code = None
            state = None
            longitude = None
            latitude = None
            zipcode = None

            iti55 = None
            iti38 = None
            iti39 = None

            partOf = None
            managingOrg = None

            status = True

            try:
                oid = sub_json_obj['Organization']['id']['value']
            except KeyError:
                print(f'Warning, missing id: {sub_json_obj}')

            try:
                name = sub_json_obj['Organization']['name']['value']
            except KeyError:
                print(f'Warning, missing name: {sub_json_obj}')
                pass

            try:
                partOf = sub_json_obj['Organization']['partOf']['identifier']['value']['value']
                partOf = partOf.replace('urn:oid:', '')
            except KeyError:
                print(f'Warning, missing part_of:{sub_json_obj}')

            try:
                managingOrg = sub_json_obj['Organization']['managingOrg']['reference']['value']
                managingOrg = managingOrg.split('/')[-1]
            except KeyError:
                print(f'Warning, missing managing org:{sub_json_obj}')

            if recursively_get_urls(oid, oid_dict) is not None:
                inherit_parent = recursively_get_urls(oid, oid_dict)
                iti55 = inherit_parent['iti55']
                iti38 = inherit_parent['iti38']
                iti39 = inherit_parent['iti39']
                oid = inherit_parent['id']

            try:
                address = sub_json_obj['Organization']['address']
                country_code = country_code_clean(
                    sub_json_obj['Organization']['address']['country']['value'])
                state = state_clean(
                    sub_json_obj['Organization']['address']['state']['value'])
                zipcode = zipcode_clean(
                    sub_json_obj['Organization']['address']['postalCode']['value'])
            except TypeError:
                # print(i,j)
                print(f'Warning, error in address formatting:{sub_json_obj}')

            except KeyError:
                print(f'Warning, missing address:{sub_json_obj}')

            try:
                longitude = sub_json_obj['Organization']['address']['extension'][
                    'valueCodeableConcept']['coding']['value']['position']['longitude'][
                    'value']
                latitude = sub_json_obj['Organization']['address']['extension'][
                    'valueCodeableConcept']['coding']['value']['position']['latitude']['value']
            except TypeError:
                # print(i,j)
                print(f'Warning, error in longitude/latitude formatting:{sub_json_obj}')

            except KeyError:
                print(f'Warning, missing latitude/longitude:{sub_json_obj}')

            try:
                status = sub_json_obj['Organization']['active']['value']
            except KeyError:
                print(f'Warning, missing status')

            final_list.append({
                'id': oid,
                'name': name,
                'resource': sub_json_obj,
                'iti55_responder': iti55,
                'iti38_responder': iti38,
                'iti39_responder': iti39,
                'address': address,
                'state': state,
                'country_code': country_code,
                'zipcode': zipcode,
                'longitude': longitude,
                'latitude': latitude,
                'part_of': partOf,
                'managing_org': managingOrg,
                'status': True
            })
    return final_list


def get_cq_db_connection():
    return psycopg2.connect(
        host=DB_HOST_NAME,
        port=5432,
        user=secret_params['db_username'],
        password=secret_params['db_password'],
        database='carequality'
    )


def process_data():
    complete_json = utils.read_data_from_s3('downloaded_json_list.json', s3_client, S3_BUCKET_NAME)
    json_list = []
    for i in range(len(complete_json)):
        json_list.append(json.loads(complete_json[i]))
    final_list = traverse_json_list(json_list)
    return final_list


def insert_table(processed_json_list, start, batch_size):
    '''
    Inserts the processed json list into the database.
    Arguments:
    processed_json_list: list of dictionaries
    '''
    json_to_insert = processed_json_list[start:start + batch_size]
    NO_URL_CASES = []
    for i in range(len(json_to_insert)):
        try:
            json_to_insert[i]['oid'] = json_to_insert[i].pop('id')
        except:
            pass
        try:
            json_to_insert[i]['resource'] = json.dumps(json_to_insert[i]['resource'])
        except:
            pass
        try:
            json_to_insert[i]['address'] = json.dumps(json_to_insert[i]['address'])
        except:
            pass
        try:
            json_to_insert[i]['longitude'] = float(json_to_insert[i]['longitude'])
        except:
            # print(LIST[i])
            json_to_insert[i]['longitude'] = None
        try:
            json_to_insert[i]['latitude'] = float(json_to_insert[i]['latitude'])
        except:
            json_to_insert[i]['latitude'] = None
        if i % 10000 == 0:
            print(i)
        # GENERATE LIST OF CASES WITH NO URLS
        if 'iti55_responder' in json_to_insert[i]:
            if json_to_insert[i]['iti55_responder'] == 'null' or json_to_insert[i][
                    'iti55_responder'] == None:
                NO_URL_CASES.append(i)
                # del LIST[i]
                continue
        else:
            NO_URL_CASES.append(i)
            # del LIST[i]
            continue
        if 'iti39_responder' in json_to_insert[i]:
            if json_to_insert[i]['iti39_responder'] == 'null' or json_to_insert[i][
                    'iti39_responder'] == None:
                NO_URL_CASES.append(i)
                # del LIST[i]
                continue
        else:
            NO_URL_CASES.append(i)
            # del LIST[i]
            continue
        if 'iti38_responder' in json_to_insert[i]:
            if json_to_insert[i]['iti38_responder'] == 'null' or json_to_insert[i][
                    'iti38_responder'] == None:
                NO_URL_CASES.append(i)
                # del LIST[i]
                continue
        else:
            NO_URL_CASES.append(i)
            # del LIST[i]
            continue

    # NO_URL_INSERT = []
    NO_URL_CASES.reverse()
    for index in NO_URL_CASES:
        del json_to_insert[index]

    for entry in json_to_insert:
        if entry.keys() != json_to_insert[0].keys():
            print(f'Error due to missing fields in the following entries {entry}')
            json_to_insert.remove(entry)

    connection = get_cq_db_connection()
    cur = connection.cursor()

    if start == 0:  # only clear table if it's the first batch of insertion
        query = f"TRUNCATE {TABLE_NAME}"
        cur.execute(query)

    # INSERT ENTRIES WITH ALL NON-NULL FIELDS
    insertion_materials = json_to_insert
    insert_columns = ', '.join(insertion_materials[0].keys())
    insert_place_holders = ', '.join(['%s'] * len(insertion_materials[0]))

    value = list(tuple(json_to_insert[i].values()) for i in range(len(json_to_insert)))
    '''
    values_text = ''
    for i in range(len(LIST)-1):
        values_text = values_text +f'({value[i]}),'
    values_text = values_text + f'({value[-1]})'
    '''

    query = f"INSERT INTO {TABLE_NAME} ({insert_columns}) VALUES ({insert_place_holders})"
    psycopg2.extras.execute_batch(cur, query, value)

    connection.commit()
    cur.close()
    connection.close()
