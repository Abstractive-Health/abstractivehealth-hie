import base64
import boto3
import os
import uuid

from lxml import etree

import utils

ENV = os.environ.get("ENV")


class ITI39Responder:
    def __init__(self, request, initiator_url=None):
        self.url = ""
        self.hcid = ""
        self.possible_urls = ["",
                              ""]
        self.documents_found = []

        # did not receive an xml request, only got initiator url (usually only for testing)
        if initiator_url:
            self.initiator_url = initiator_url
            self.request = None
        else:
            self.request = request
            root = self.request
            self.initiator_url = root.find(
                './/{*}ReplyTo/{*}Address').text if root.find('.//{*}ReplyTo/{*}Address') else None

            # check that it's to us
            to_element = root.find('.//*/{*}To').text
            if to_element not in self.possible_urls:
                raise Exception(f"request is not to us, it's to {to_element}")

    def write_request(self):
        s3_write_client = boto3.client('s3', endpoint_url="https://s3.amazonaws.com/")
        s3_write_client.put_object(Body=etree.tostring(self.request),
                                   Bucket=f"",
                                   Key="39request/" + str(uuid.uuid4()) + ".xml")
        return "write success"

    def process_xca_retrieve_documents_request(self):
        '''
        Get the patient ids which are in a list
        '''
        root = self.request

        document_request_elements = root.findall('.//{*}DocumentRequest')

        metadata, org_hcids = [], set()
        for document_request_element in document_request_elements:
            repo_id = document_request_element.find('.//{*}RepositoryUniqueId').text
            hcid = document_request_element.find('.//{*}HomeCommunityId').text
            document_unique_id = document_request_element.find('.//{*}DocumentUniqueId').text

            if hcid[:8] == 'urn:oid:':
                hcid = hcid[8:]

            # if hcid == self.hcid:
            #     metadata.append((hcid, repo_id, document_unique_id))
            # hcid now should be our org_hcid
            if utils.is_valid_org_hcid(hcid):
                org_hcids.add(hcid)
                metadata.append((hcid, repo_id, document_unique_id))

        if len(org_hcids) == 1:
            self.org_hcid = org_hcids.pop()
        else:
            self.org_hcid = None

        self.metadata = metadata
        print("metadata:", metadata)
        return metadata

    def search_db_for_documents(self):
        '''
        sql queries redacted
        '''
        documents_found = []
        for hcid, repo_id, document_unique_id in self.metadata:

            self.cur.execute(
                f'''''')
            result = self.cur.fetchone()
            if result:
                print("found summary for,", document_unique_id)

                # TODO: change the cda convertion to a call to summaries lambda
                summary, pid, provider_id = result[0], result[1], result[2]
                patient_metadata = utils.get_patient_metadata(self.org_id, pid)
                print("patient_metadata:", patient_metadata)
                provider_metadata = utils.get_provider_metadata(provider_id)
                print("provider_metadata:", provider_metadata)
                org_metadata = utils.get_org_metadata(self.org_id)
                print("org_metadata:", org_metadata)
                xml_str = self.generate_cda_xml(
                    pid, patient_metadata, provider_metadata, org_metadata, summary)

                # update converted xml to table
                self.cur.execute('''''',
                                 (xml_str, document_unique_id))

                xml_bytes_doc = bytes(xml_str, 'utf-8')
                documents_found.append({'hcid': hcid, 'repo_id': repo_id,
                                        'document_unique_id': document_unique_id,
                                        'document': base64.b64encode(xml_bytes_doc)})
        self.documents_found = documents_found
        print("number of documents found,", len(documents_found))
        return documents_found

    def generate_cda_xml(self, pid, patient_metadata, provider_metadata, org_metadata, summary):
        '''
        Fill CDA template with patient, provider and organization metadata,
        and base64-encoded narrative content
        '''
        xml_template = open('template/cda_template.xml', 'r').read()
        xml_bytes = xml_template.encode('utf-8')
        root = etree.fromstring(xml_bytes)

        # namespace map
        ns = {'cda': 'urn:hl7-org:v3'}

        # fill in patient metadata
        patient_id = root.xpath('.//cda:patientRole/cda:id', namespaces=ns)
        patient_id[0].attrib['extension'] = pid
        patient_id[0].attrib['root'] = self.org_hcid

        given_name = root.xpath('.//cda:given', namespaces=ns)
        family_name = root.xpath('.//cda:family', namespaces=ns)
        given_name[0].text = patient_metadata["given"]
        family_name[0].text = patient_metadata["family"]

        gender = root.xpath('.//cda:administrativeGenderCode', namespaces=ns)
        gender[0].attrib['code'] = patient_metadata["gender"]
        gender[0].attrib['displayName'] = utils.GENDER_CODE_DISPLAY_MAP[patient_metadata["gender"]]

        birth_time = root.xpath('.//cda:birthTime', namespaces=ns)
        birth_time[0].attrib['value'] = patient_metadata["birthtime"].replace('-', '')

        street_address = root.xpath('.//cda:streetAddressLine', namespaces=ns)
        city = root.xpath('.//cda:city', namespaces=ns)
        state = root.xpath('.//cda:state', namespaces=ns)
        postal_code = root.xpath('.//cda:postalCode', namespaces=ns)
        country = root.xpath('.//cda:country', namespaces=ns)
        if street_address and patient_metadata.get("line"):
            street_address[0].text = patient_metadata["line"]
        if city and patient_metadata.get("city"):
            city[0].text = patient_metadata["city"]
        if state and patient_metadata.get("state"):
            state[0].text = patient_metadata["state"]
        if postal_code and patient_metadata.get("postal_code"):
            postal_code[0].text = patient_metadata["postal_code"]
        if country and patient_metadata.get("country"):
            country[0].text = patient_metadata["country"]

        # fill in org metadata
        org_id = root.xpath('.//cda:providerOrganization/cda:id', namespaces=ns)
        org_name = root.xpath('.//cda:providerOrganization/cda:name', namespaces=ns)
        org_id[0].attrib['root'] = self.org_hcid
        org_name[0].text = self.org_name

        if org_metadata.get("phone_number"):
            org_phone = root.xpath('.//cda:providerOrganization/cda:telecom', namespaces=ns)
            org_phone[0].attrib['value'] = org_metadata["phone_number"]

        org_address = org_metadata["address"]
        if org_address:
            org_street_address = root.xpath(
                './/cda:providerOrganization/cda:addr/cda:streetAddressLine', namespaces=ns)
            org_city = root.xpath('.//cda:providerOrganization/cda:addr/cda:city', namespaces=ns)
            org_state = root.xpath('.//cda:providerOrganization/cda:addr/cda:state', namespaces=ns)
            org_postal_code = root.xpath(
                './/cda:providerOrganization/cda:addr/cda:postalCode', namespaces=ns)
            org_country = root.xpath(
                './/cda:providerOrganization/cda:addr/cda:country', namespaces=ns)
            if org_address.get("line"):
                org_street_address[0].text = org_address["line"]
            if org_address.get("city"):
                org_city[0].text = org_address["city"]
            if org_address.get("state"):
                org_state[0].text = org_address["state"]
            if org_address.get("postal_code"):
                org_postal_code[0].text = org_address["postal_code"]
            if org_address.get("country"):
                org_country[0].text = org_address["country"]

        # fill in provider metadata
        provider_id = root.xpath('.//cda:assignedAuthor/cda:id', namespaces=ns)
        provider_id[0].attrib['root'] = provider_metadata["npi"]

        specialty_code = root.xpath('.//cda:assignedAuthor/cda:code', namespaces=ns)
        provider_specialty = provider_metadata["specialty"]
        specialty_code[0].attrib['code'] = utils.SPECIALTY_CODE_DISPLAY_MAP[provider_specialty]["code"]
        specialty_code[0].attrib['displayName'] = utils.SPECIALTY_CODE_DISPLAY_MAP[provider_specialty]["display_name"]

        provider_zipcode = root.xpath(
            './/cda:assignedAuthor/cda:addr/cda:postalCode', namespaces=ns)
        provider_zipcode[0].text = provider_metadata["zip_code"]
        provider_country = root.xpath('.//cda:assignedAuthor/cda:addr/cda:country', namespaces=ns)
        provider_country[0].text = provider_metadata["country_code"]

        provider_phone = root.xpath('.//cda:assignedAuthor/cda:telecom', namespaces=ns)
        provider_phone[0].attrib['value'] = provider_metadata["phone_number"]

        provider_first_name = root.xpath(
            './/cda:assignedAuthor/cda:assignedPerson/cda:name/cda:given', namespaces=ns)
        provider_last_name = root.xpath(
            './/cda:assignedAuthor/cda:assignedPerson/cda:name/cda:family', namespaces=ns)
        provider_first_name[0].text = provider_metadata["first_name"]
        provider_last_name[0].text = provider_metadata["last_name"]

        # fill in base64-encoded narrative content
        non_xml_body_text = utils.json2xml(summary)
        non_xml_body_element = root.xpath('.//cda:nonXMLBody/cda:text', namespaces=ns)
        encoded_text = base64.b64encode(non_xml_body_text.encode('utf-8')).decode('utf-8')
        non_xml_body_element[0].text = encoded_text

        # convert back to string
        return etree.tostring(root, pretty_print=True, encoding='utf-8').decode('utf-8')

    def generate_retrieve_document_set_response(self):
        '''
        documents_found is a list of [(Home Community ID, Repository ID, and Document unique ID)...]
        '''
        RetrieveDocumentSetResponse = etree.Element(
            '{urn:ihe:iti:xds-b:2007}RetrieveDocumentSetResponse')
        RegistryResponse = etree.SubElement(
            RetrieveDocumentSetResponse,
            '{urn:oasis:names:tc:ebxml-regrep:xsd:rs:3.0}RegistryResponse')
        RegistryResponse.set('status',
                             'urn:oasis:names:tc:ebxml-regrep:ResponseStatusType:Success')

        for document in self.documents_found:
            DocumentResponse = etree.SubElement(
                RetrieveDocumentSetResponse,
                '{urn:ihe:iti:xds-b:2007}DocumentResponse'
            )
            HomeCommunityId = etree.SubElement(
                DocumentResponse,
                '{urn:ihe:iti:xds-b:2007}HomeCommunityId'
            )
            HomeCommunityId.text = document['hcid']
            RepositoryUniqueId = etree.SubElement(
                DocumentResponse,
                '{urn:ihe:iti:xds-b:2007}RepositoryUniqueId'
            )
            RepositoryUniqueId.text = document['repo_id']
            DocumentUniqueId = etree.SubElement(
                DocumentResponse,
                '{urn:ihe:iti:xds-b:2007}DocumentUniqueId'
            )
            DocumentUniqueId.text = document['document_unique_id']
            mimeType = etree.SubElement(
                DocumentResponse,
                '{urn:ihe:iti:xds-b:2007}mimeType'
            )
            mimeType.text = "text/xml"
            Document = etree.SubElement(
                DocumentResponse,
                '{urn:ihe:iti:xds-b:2007}Document'
            )
            Document.text = document['document']

        self.response_body = RetrieveDocumentSetResponse

        return self.response_body

    def generate_response_body(self):
        '''
        documents_found is a list of [(Home Community ID, Repository ID, and Document unique ID)...]
        '''
        self.write_request()
        self.process_xca_retrieve_documents_request()

        # search db
        # if self.org_hcid:
        #     self.org_id, self.org_name = utils.get_org_id_and_name_from_org_hcid(self.org_hcid)
        #     if self.org_id:
        #         conn = utils.get_db_connection(utils.get_org_db_name_from_org_id(self.org_id))
        #         self.cur = conn.cursor()
        #         self.search_db_for_documents()
        #         self.cur.close()
        #         conn.commit()
        #         conn.close()

        self.generate_retrieve_document_set_response()
        return self.response_body
