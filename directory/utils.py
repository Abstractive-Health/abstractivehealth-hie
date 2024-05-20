def validate_endpoint_dict(endpoint, exclude=set()):
    '''
    an endpoint dict looks like this, and we want to make sure the urls are actually urls
    {
            'oid': oid,
            'name': endpoint[1],
            'iti55_responder': endpoint[2],
            'iti38_responder': endpoint[3],
            'iti39_responder': endpoint[4],
            'status': endpoint[5]
    }
    '''
    for key in ['iti55_responder', 'iti38_responder', 'iti39_responder']:
        if endpoint[key] is None or not (
                endpoint[key].startswith('http') or endpoint[key].startswith('https')):
            return None
        elif endpoint['name'] in exclude:
            return None
    return endpoint
