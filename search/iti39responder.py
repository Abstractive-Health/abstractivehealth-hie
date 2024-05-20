import base64
import boto3
import os
import uuid

from lxml import etree

import utils

ENV = os.environ.get("ENV")


class ITI39Responder:
    def __init__(self, cur, request, initiator_url=None):
        self.url = ""
        self.cur = cur
        self.hcid = ""
        self.possible_urls = []

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
        '''
        log request
        '''
        return "write success"

    def process_xca_retrieve_documents_request(self):
        '''
        Get the patient ids which are in a list
        '''
        root = self.request

        document_request_elements = root.findall('.//{*}DocumentRequest')

        metadata = []
        for document_request_element in document_request_elements:
            repo_id = document_request_element.find('.//{*}RepositoryUniqueId').text
            hcid = document_request_element.find('.//{*}HomeCommunityId').text
            document_unique_id = document_request_element.find('.//{*}DocumentUniqueId').text

            if hcid[:8] == 'urn:oid:':
                hcid = hcid[8:]

            if hcid == self.hcid:
                metadata.append((hcid, repo_id, document_unique_id))

        self.metadata = metadata
        print("metadata:", metadata)
        return metadata

    def search_db_for_documents(self):
        '''
        In a list of fhir tables,
        find actual documents associated with document_unique_id
        return dicts of {'hcid': hcid, 'repo_id': repo_id, 'document_unique_id': document_unique_id, 'document': document}
        document needs to be base64 encoded
        '''
        tables = []
        documents_found = []
        for hcid, repo_id, document_unique_id in self.metadata:
            our_doc_id = document_unique_id

            print("searching for doc_id across tables:", our_doc_id)
            for table in tables:
                self.cur.execute(f"SELECT resource FROM {table} WHERE id = '{our_doc_id}'")
                resource = self.cur.fetchone()
                if resource is not None:
                    print("found resource for,", document_unique_id)
                    xml_str = utils.json2xml(list(resource))
                    xml_bytes_doc = bytes(xml_str, 'utf-8')

                    documents_found.append({'hcid': hcid, 'repo_id': repo_id,
                                            'document_unique_id': document_unique_id,
                                            'document': base64.b64encode(xml_bytes_doc)})
        self.documents_found = documents_found
        print("number of documents found,", len(documents_found))
        return documents_found

    def generate_response_body(self):
        '''
        documents_found is a list of [(Home Community ID, Repository ID, and Document unique ID)...]
        '''
        self.write_request()
        self.process_xca_retrieve_documents_request()
        self.search_db_for_documents()
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
