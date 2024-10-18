import boto3
import json
import os

ENV = os.environ.get("ENV")
S3_BUCKET_NAME = ''
s3_client = boto3.client('s3', endpoint_url="https://s3.amazonaws.com/")


def validate_endpoint_dicts(endpoints, exclude=set()):
    '''
    each endpoint dict looks like this, and we want to make sure the urls are actually urls
    {
            'oid': oid,
            'name': endpoint[1],
            'iti55_responder': endpoint[2],
            'iti38_responder': endpoint[3],
            'iti39_responder': endpoint[4]
    }
    returns a deduplicated list of validated endpoints. No None
    '''
    total_exclude = exclude.copy()
    valid_endpoint_dicts = []

    for endpoint in endpoints:
        # post process oid
        oid = endpoint[0] if 'urn:oid:' not in endpoint[0] else endpoint[0].split('urn:oid:')[1]
        endpoint = {
            'oid': oid,
            'name': endpoint[1],
            'iti55_responder': endpoint[2],
            'iti38_responder': endpoint[3],
            'iti39_responder': endpoint[4]
        }

        url_valid = True
        for key in ['iti55_responder', 'iti38_responder', 'iti39_responder']:
            if endpoint[key] is None or not (
                    endpoint[key].startswith('http') or endpoint[key].startswith('https')):
                url_valid = False
                break

        if url_valid:
            if (endpoint['oid'], endpoint['iti55_responder']) not in total_exclude:
                # deduplicate within this set, i.e. between state and radius search
                total_exclude.add((endpoint['oid'], endpoint['iti55_responder']))
                valid_endpoint_dicts.append(endpoint)

    return valid_endpoint_dicts


def read_data_from_s3(file_name, s3_client, bucket_name):
    s3_object = s3_client.get_object(Bucket=bucket_name, Key=file_name)
    data = json.loads(s3_object['Body'].read().decode('utf-8'))
    return data
