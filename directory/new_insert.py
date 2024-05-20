import boto3
import json
import os

import psycopg2

ENV = os.environ.get("ENV")
secretsmanager = boto3.client('secretsmanager')
secret_id = ""
secret_params = {}
s3_client = boto3.client('s3', endpoint_url="https://s3.amazonaws.com/")

# host used to connect to PostgreSQL
DB_HOST_NAME = ''
S3_BUCKET_NAME = ''
CQPROD_STU3_TABLE_NAME = ''

def get_cq_db_connection():
    return psycopg2.connect(
        host=DB_HOST_NAME,
        port=,
        user=secret_params['db_username'],
        password=secret_params['db_password'],
        database=''
    )


def read_data_from_s3(file_name):
    s3_object = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=file_name)
    data = json.loads(s3_object['Body'].read().decode('utf-8'))
    return data


def strip_oid(oid):
    return oid if 'urn:oid:' not in oid else oid.split('urn:oid:')[1]


def strip_org_name(managing_org):
    '''
    examples input: org.sequoiaproject.fhir.stu3/Organization/Broker's Broker
    '''
    if '/' in managing_org:
        return managing_org.split('/')[-1]
    else:
        return managing_org


def get_part_of(resource):
    try:
        if type(resource) is str:
            resource = json.loads(resource)
        stripped = strip_oid(
            resource['Organization']['partOf']['identifier']['value']['value'])
        if len(stripped) == 0:
            return None
        else:
            return stripped
    except:
        return None


def get_active(resource):
    try:
        if type(resource) is str:
            resource = json.loads(resource)

        active_status = resource['Organization']['active']['value']
        if type(active_status) is bool:
            return active_status

        if active_status.lower() == 'true':
            return True
        else:
            return False
    except:
        return False


def get_managing_org(resource):
    try:
        if type(resource) is str:
            resource = json.loads(resource)
        stripped = strip_org_name(
            resource['Organization']['managingOrg']['reference']['value'])
        if len(stripped) == 0:
            return None
        else:
            return stripped
    except:
        return None


def update_table(cur, table_name, update_data, condition_column, condition_value):
    # Construct the SET part of the query
    set_clause = ', '.join(
        [f"{key} = %({key})s" for key in update_data.keys()])

    # Construct the UPDATE query
    query = f"""
    UPDATE {table_name}
    SET {set_clause}
    WHERE {condition_column} = %({condition_column})s
    """

    # Add the condition value to the update_data dictionary
    update_data[condition_column] = condition_value

    # Execute the query
    cur.execute(query, update_data)
    return


def insert_one_org_one_iteration(org_info, cur, second_loop_onwards=False):
    inherited_parent_urls = 0  # did not inherit parent url
    if not second_loop_onwards:
        insertion_materials = {"oid": strip_oid(org_info['oid']),
                               "name": org_info['name'],
                               "resource": org_info['resource'],
                               "iti55_responder": org_info['iti55_responder'],
                               "iti38_responder": org_info['iti38_responder'],
                               "iti39_responder": org_info['iti39_responder'],
                               "address": org_info['address'],
                               "longitude": org_info['longitude'],
                               "latitude": org_info['latitude'],
                               "zipcode": org_info['zipcode'],
                               "country_code": org_info['country_code'],
                               "part_of": get_part_of(org_info['resource']),
                               "managing_org": get_managing_org(org_info['resource']),
                               "status": get_active(org_info['resource'])
                               }
    else:
        cur.execute(f"""SELECT
                    oid,
                    name,
                    resource,
                    iti55_responder,
                    iti38_responder,
                    iti39_responder,
                    address,
                    longitude,
                    latitude,
                    zipcode,
                    country_code,
                    part_of,
                    managing_org,
                    status
                    FROM {CQPROD_STU3_TABLE_NAME} WHERE
                    oid = '{org_info['oid']}' or oid = 'urn:oid:{org_info['oid']}'"""
                    )
        entry = cur.fetchone()
        insertion_materials = {
            'oid': entry[0],
            'name': entry[1],
            'resource': entry[2],
            'iti55_responder': entry[3],
            'iti38_responder': entry[4],
            'iti39_responder': entry[5],
            'address': entry[6],
            'longitude': entry[7],
            'latitude': entry[8],
            'zipcode': entry[9],
            'country_code': entry[10],
            'part_of': entry[11],
            'managing_org': entry[12],
            'status': entry[13]
        }
        for key, value in insertion_materials.items():
            if type(value) is dict or type(value) is bytes:
                insertion_materials[key] = json.dumps(value)
            else:
                insertion_materials[key] = value

    if insertion_materials['part_of'] is not None:
        # inherit managing_org from parent
        cur.execute(
            f"SELECT managing_org FROM {CQPROD_STU3_TABLE_NAME} WHERE oid = '{insertion_materials['part_of']}' or oid = 'urn:oid:{insertion_materials['part_of']}'")
        inherited_managing_org = cur.fetchone()
        if inherited_managing_org is not None:
            insertion_materials['managing_org'] = inherited_managing_org[0]
        # if the urls aren't all there
        if not all([insertion_materials['iti55_responder'],
                    insertion_materials['iti38_responder'],
                    insertion_materials['iti39_responder']]):
            cur.execute(
                f"SELECT iti55_responder, iti38_responder, iti39_responder FROM {CQPROD_STU3_TABLE_NAME} WHERE oid = '{insertion_materials['part_of']}' or oid = 'urn:oid:{insertion_materials['part_of']}'")
            parent_urls = cur.fetchone()
            if parent_urls is not None and all(
                    [parent_url is not None for parent_url in parent_urls]):
                inherited_parent_urls = 1  # did inherit parent url
                insertion_materials['iti55_responder'] = parent_urls[0]
                insertion_materials['iti38_responder'] = parent_urls[1]
                insertion_materials['iti39_responder'] = parent_urls[2]
                insertion_materials['oid'] = insertion_materials['part_of']

    if not second_loop_onwards:
        insert_columns = ', '.join(insertion_materials.keys())
        insert_place_holders = ', '.join(
            ['%s'] * len(insertion_materials))  # placeholder for each value
        insert_query = f"INSERT INTO {CQPROD_STU3_TABLE_NAME} ({insert_columns}) VALUES ({insert_place_holders})"
        query = insert_query
        value = tuple(insertion_materials.values())
        cur.execute(query, value)

    else:
        update_table(cur, CQPROD_STU3_TABLE_NAME, insertion_materials,
                     'oid', "urn:oid:"+insertion_materials['oid'])

    return inherited_parent_urls


def clean_up_final_entries(cur):
    # loop through all entries, and clean up all that do not have:
    # all 3 urls, "longitude", "latitude", "zipcode"
    illegal_count = 0
    cur.execute(
        f"SELECT oid, iti55_responder, iti38_responder, iti39_responder, longitude, latitude, zipcode FROM {CQPROD_STU3_TABLE_NAME}")
    entries = cur.fetchall()
    for entry in entries:
        illegal_count += 1
        if not all(entry):
            cur.execute(
                f"DELETE FROM {CQPROD_STU3_TABLE_NAME} WHERE oid = '{entry[0]}'")

    print(f"cleaned up {illegal_count} entries")
    return


def insert_prod_directory():
    # TODO: instead of reading data from s3, pull live from the directory url
    connection = get_cq_db_connection()
    connection.autocommit = True
    cur = connection.cursor()
    cur.execute(f"DELETE FROM {CQPROD_STU3_TABLE_NAME};")

    directory_data = read_data_from_s3('')

    inheritance_history = []
    number_of_entries_inheriting_urls = 0

    # loop 1, with original data
    for org_info in directory_data:
        number_of_entries_inheriting_urls += insert_one_org_one_iteration(
            org_info, cur)
    print(f"number of entries inheriting urls: {number_of_entries_inheriting_urls} on iteration 0")
    inheritance_history.append(number_of_entries_inheriting_urls)

    # loop 2-5, with what's in the db
    for i in range(1, 5):
        # select entire row with every column name
        if inheritance_history[-1] == 0:
            break  # early break if we find we're not inheriting anymore
        number_of_entries_inheriting_urls = 0
        cur.execute(f"SELECT oid FROM {CQPROD_STU3_TABLE_NAME}")
        for entry in cur.fetchall():
            number_of_entries_inheriting_urls += insert_one_org_one_iteration(
                {"oid": entry[0]}, cur, second_loop_onwards=True)
        print(
            f"number of entries inheriting urls: {number_of_entries_inheriting_urls} on iteration {i}")
        inheritance_history.append(number_of_entries_inheriting_urls)

    print("inheritance history:", inheritance_history)
    clean_up_final_entries(cur)

    connection.close()

    return
