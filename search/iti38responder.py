import boto3
import os
import uuid

from lxml import etree

ENV = os.environ.get("ENV")


class ITI38Responder:
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
            self.returntype = root.find('.//{*}ResponseOption').attrib['returnType']

            # check that it's to us
            to_element = root.find('.//*/{*}To').text
            if to_element not in self.possible_urls:
                raise Exception(f"request is not to us, it's to {to_element}")

    def write_request(self):
        '''
        log request
        '''

        return "write success"

    def process_xca_find_documents_request(self):
        '''
        Get the patient ids which are in a list
        '''
        root = self.request

        # Find all elements with the specified namespace and tag
        patient_id_elements = root.findall(
            './/{*}Slot[@name="$XDSDocumentEntryPatientId"]/{*}ValueList/{*}Value')

        # Extract the values so that we have list of strings
        patient_ids = [eval(element.text).split('^^^')[0] for element in patient_id_elements]

        self.patient_ids = patient_ids

    def search_db_for_documents_metadata(self):
        '''
        Searches our database for documents that match the pids in self.patient_ids, not including summaries.
        Returns a list of [(Home Community ID, Repository ID, and Document unique ID)...] metadata for the documents found
        DO NOT RETURN THE ACTUAL CONTENTS OF THE DOCUMENTS
        '''

        # list to store list of [(Home Community ID, Repository ID, and Document unique ID)...] metadata
        hcid = self.hcid
        rid = self.hcid

        # list of fhir tables to search for documents
        document_locations = [
           
        ]

        results = set()  # set of tuples of (hcid, rid, document_id)
        for pid in self.patient_ids:
            for table in document_locations:
                sql_queries = [
                    '''SELECT id, resource FROM {table} WHERE resource->'patient' @> \'{{"id": "{pid}"}}\''''.
                    format(table=table, pid=pid),
                    '''SELECT id, resource FROM {table} WHERE resource->'subject' @> \'{{"id": "{pid}"}}\''''.
                    format(table=table, pid=pid),
                    '''SELECT id, resource FROM {table} WHERE resource @> \'{{"patientFhirId": "{pid}"}}\''''.
                    format(table=table, pid=pid)]  # apparent variation in db
                for query in sql_queries:
                    self.cur.execute(query)
                    doc_ids_resources_for_patients = self.cur.fetchall()
                    for (doc_id, resource) in doc_ids_resources_for_patients:
                        loinc_code = self.get_loinc_from_resource(resource)
                        format_code, format_system = self.get_format_code_and_system_from_resource(
                            resource)
                        hcf, hcf_system = self.get_hcf_and_system_from_resource(resource)
                        results.add((hcid, rid, doc_id, pid, table, loinc_code,
                                    format_code, format_system, hcf, hcf_system))

        self.documents_found = list(results)
        print("documents founds have the following ids", results)
        return results

    def get_loinc_from_resource(self, resource):
        try:
            return [category['coding'][0]['code']
                    for category in resource['category']
                    if category['coding'][0]['system'] == 'http://loinc.org'][0]
        except:
            try:
                return [code['code']
                        for code in resource['type']['coding']
                        if code['system'] == 'http://loinc.org'][0]
            except:
                return ""

    def get_format_code_and_system_from_resource(self, resource):
        try:
            return resource['content'][0]['format']['code'], resource['content'][0]['format'][
                'system']
        except:
            return "", ""

    def get_hcf_and_system_from_resource(self, resource):
        # TODO: implement based on a fhir resource that actually has this.
        return "", ""

    def build_classification_object(
            self, registry_object_id_long, classification_scheme, code, system):
        classification = etree.Element(
            '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}Classification',
            id='urn:uuid:' + str(uuid.uuid4()),
            objectType="urn:oasis:names:tc:ebxml-regrep:ObjectType:RegistryObject:Classification",
            classificationScheme=classification_scheme,
            classifiedObject=registry_object_id_long,
            nodeRepresentation=code
        )
        classification_slot = etree.SubElement(
            classification,
            '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}Slot',
            name='codingScheme'
        )
        classification_slot_value_list = etree.SubElement(
            classification_slot,
            '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}ValueList'
        )
        classification_slot_value = etree.SubElement(
            classification_slot_value_list,
            '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}Value'
        )
        classification_slot_value.text = system
        return classification

    def generate_response_body(self):
        '''
        documents_found is a list of [(Home Community ID, Repository ID, and Document unique ID...)...]
        '''
        self.write_request()
        self.process_xca_find_documents_request()
        self.search_db_for_documents_metadata()
        AdhocQueryResponse = etree.Element(
            '{urn:oasis:names:tc:ebxml-regrep:xsd:query:3.0}AdhocQueryResponse')
        AdhocQueryResponse.set(
            'status', 'urn:oasis:names:tc:ebxml-regrep:ResponseStatusType:Success')
        RegistryObjectList = etree.SubElement(
            AdhocQueryResponse, '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}RegistryObjectList')

        # object ref is useless
        if self.returntype == "ObjectRef":
            for hcid, repo_id, doc_id, pid in self.documents_found:
                ObjectRef = etree.SubElement(
                    RegistryObjectList, '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}ObjectRef')
                ObjectRef.set('id', "urn:uuid:" + doc_id)
                ObjectRef.set('home', "urn:oid:" + hcid)
        elif self.returntype == "LeafClass":
            for hcid, repo_id, doc_id, pid, doc_name, loinc, format_code, format_system, hcf, hcf_system \
                    in self.documents_found:

                # make pid slot and external identifier element
                pid_concat = pid + "^^^&" + hcid + "&ISO"

                patient_slot = etree.Element(
                    '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}Slot', name='sourcePatientId')
                v_l = etree.SubElement(
                    patient_slot,
                    '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}ValueList'
                )
                v = etree.SubElement(v_l,
                                     '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}Value'
                                     )
                v.text = pid_concat

                repo_slot = etree.Element(
                    '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}Slot',
                    name='repositoryUniqueId'
                )
                rv_l = etree.SubElement(
                    repo_slot,
                    '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}ValueList'
                )
                rv = etree.SubElement(rv_l,
                                      '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}Value'
                                      )
                rv.text = hcid

                registry_object_id_long = "urn:uuid:" + str(uuid.uuid4())

                EI_patient_slot = etree.Element(
                    '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}ExternalIdentifier',
                    id='urn:uuid:' + str(uuid.uuid4()),
                    lid='urn:uuid:' + str(uuid.uuid4()),
                    objectType='urn:oasis:names:tc:ebxml-regrep:ObjectType:RegistryObject:ExternalIdentifier',
                    registryObject=registry_object_id_long,
                    identificationScheme='urn:uuid:58a6f841-87b3-4a3e-92fd-a8ffeff98427',
                    value=pid_concat)
                n = etree.SubElement(
                    EI_patient_slot, '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}Name')
                l_s = etree.SubElement(
                    n, '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}LocalizedString',
                    charset='UTF-8', value='XDSDocumentEntry.patientId')

                EI_object_slot = etree.Element(
                    '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}ExternalIdentifier',
                    id='urn:uuid:' + str(uuid.uuid4()),
                    lid='urn:uuid:' + str(uuid.uuid4()),
                    objectType='urn:oasis:names:tc:ebxml-regrep:ObjectType:RegistryObject:ExternalIdentifier',
                    registryObject=registry_object_id_long,
                    identificationScheme='urn:uuid:2e82c1f6-a085-4c72-9da3-8640a32e42ab',
                    value=doc_id)
                n = etree.SubElement(
                    EI_object_slot,
                    '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}Name'
                )
                l_s = etree.SubElement(
                    n,
                    '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}LocalizedString',
                    charset='UTF-8',
                    value='XDSDocumentEntry.uniqueId'
                )

                doc_name_element = etree.Element(
                    "{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}Name"
                )
                d_s = etree.SubElement(
                    doc_name_element,
                    '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}LocalizedString',
                    charset='UTF-8',
                    value=doc_name
                )

                classification_classCode = self.build_classification_object(
                    registry_object_id_long,
                    "urn:uuid:41a5887f-8865-4c09-adf7-e362475b143a",
                    loinc,
                    "2.16.840.1.113883.6.1"
                )
                classification_formatCode = self.build_classification_object(
                    registry_object_id_long,
                    "urn:uuid:a09d5840-386c-46f2-b5ad-9c3699a4309d",
                    format_code,
                    format_system)
                classification_confidentialityCode = self.build_classification_object(
                    registry_object_id_long,
                    "urn:uuid:f4f85eac-e6cb-4883-b524-f2705394840f",
                    "N",
                    "2.16.840.1.113883.5.25"
                )
                if hcf and hcf_system:
                    classification_hcfCode = self.build_classification_object(
                        registry_object_id_long,
                        "urn:uuid:93606bcf-9494-43ec-9b4e-a7748d1a838d",
                        hcf,
                        hcf_system
                    )
                else:
                    classification_hcfCode = None

                ExtrinsicObject = etree.SubElement(
                    RegistryObjectList,
                    '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}ExtrinsicObject'
                )
                ExtrinsicObject.set('id', registry_object_id_long)
                ExtrinsicObject.set('home', "urn:oid:" + hcid)
                ExtrinsicObject.set('mimeType', "text/xml")
                ExtrinsicObject.set('isOpaque', "false")
                ExtrinsicObject.set('status', "urn:oasis:names:tc:ebxml-regrep:StatusType:Approved")
                ExtrinsicObject.append(patient_slot)
                ExtrinsicObject.append(repo_slot)
                ExtrinsicObject.append(doc_name_element)

                ExtrinsicObject.append(classification_classCode)
                ExtrinsicObject.append(classification_formatCode)
                ExtrinsicObject.append(classification_confidentialityCode)
                if classification_hcfCode:
                    ExtrinsicObject.append(classification_hcfCode)

                ExtrinsicObject.append(EI_patient_slot)
                ExtrinsicObject.append(EI_object_slot)

            RegistryPackage = etree.SubElement(
                RegistryObjectList,
                '{urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0}RegistryPackage'
            )
            RegistryPackage.set('home', "urn:oid:" + hcid)
            RegistryPackage.set('id', "urn:uuid:" + str(uuid.uuid4()))
            RegistryPackage.set(
                'objectType',
                "urn:oasis:names:tc:ebxml-regrep:ObjectType:RegistryPackage"
            )
            RegistryPackage.set(
                'status',
                "urn:oasis:names:tc:ebxml-regrep:StatusType:Approved"
            )

        self.response_body = AdhocQueryResponse
        return self.response_body
