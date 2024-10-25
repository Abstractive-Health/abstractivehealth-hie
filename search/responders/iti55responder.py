import boto3
import json
import os
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from lxml import etree
from zeep import Client, Settings, xsd
from zeep.exceptions import Fault
from zeep.transports import Transport

import utils

ENV = os.environ.get("ENV")


class ITI55Responder:
    def __init__(self, request, initiator_url=None):
        self.url = ""
        self.possible_urls = ["",
                              ""]
        self.hcid = ''
        self.search_result = []

        # did not receive an xml request, only got initiator url (usually only for testing)
        if initiator_url:
            self.initiator_url = initiator_url
            self.request = None
        else:
            self.request = request
            root = self.request
            self.initiator_url = root.find(
                './/{*}ReplyTo/{*}Address').text if root.find('.//{*}ReplyTo/{*}Address') else None
            # extension and root of query id
            self.query_id_element = root.find(
                './/{*}PRPA_IN201305UV02/{*}controlActProcess/{*}queryByParameter/{*}queryId')
            self.query_by_parameter_element = root.find(
                './/{*}PRPA_IN201305UV02/{*}controlActProcess/{*}queryByParameter')  # the query we need to regurgitate
            self.receiver_hcid = root.find(
                './/{*}PRPA_IN201305UV02/{*}sender/{*}device/{*}id').get("root")  # the receiver's hcid
            self.org_hcid = root.find(
                './/{*}PRPA_IN201305UV02/{*}receiver/{*}device/{*}asAgent/{*}representedOrganization/').get("root")

            # check if org_hcid is valid
            if not utils.is_valid_org_hcid(self.org_hcid):
                self.org_hcid = None

            # check that it's to us
            to_element = root.find('.//*/{*}To').text
            if to_element not in self.possible_urls:
                raise Exception(f"request is not to us, it's to {to_element}")

    def write_request(self):
        # s3_write_client = boto3.client('s3', endpoint_url="https://s3.amazonaws.com/")
        # s3_write_client.put_object(Body=etree.tostring(self.request),
        #                            Bucket=f"", Key="55request/" +
        #                            str(uuid.uuid4()) + ".xml")
        return "write success"

    def process_xcpd_request(self):
        '''
        extract demographic parameters from the request (https://profiles.ihe.net/ITI/TF/Volume2/ITI-55.html 3.55.4.1.2.1)
        returns demographic parameters as a dictionary
        '''
        # Parse response - zeep.client to interact with request, wsdl.types - access to types in wsdl, desterialize - parse and turn into python obj
        # only process the body of the request
        root = self.request.find(".//PRPA_IN201305UV02")

        # Define the namespace used in the XML
        ns = {'hl7': 'urn:hl7-org:v3'}
        root = self.request

        # NEED TO HANDEL EACH CASE IF ISNT ABLE TO EXTRACT
        # keys are parameter names, values are parameter xpaths and extraction types
        params = {
            'given': ('.//{*}livingSubjectName/{*}value/{*}given', 'text'),
            'family': ('.//{*}livingSubjectName/{*}value/{*}family', 'text'),
            'gender': ('.//{*}livingSubjectAdministrativeGender/{*}value', 'get("code")'),
            'birthtime': ('.//{*}livingSubjectBirthTime/{*}value', 'get("value")'),
            'city': ('.//{*}patientAddress/{*}value/{*}city', 'text'),
            'state': ('.//{*}patientAddress/{*}value/{*}state', 'text'),
            'line': ('.//{*}patientAddress/{*}value/{*}streetAddressLine', 'text'),
            'country': ('.//{*}patientAddress/{*}value/{*}country', 'text'),
            'postal_code': ('.//{*}patientAddress/{*}value/{*}postalCode', 'text'),
            'mmname': ('.//{*}mothersMaidenName/{*}value/{*}family', 'text'),
            'patient_telecom': ('.//{*}patientTelecom/{*}value', 'get("value")'),
            'telecom_use': ('.//{*}patientTelecom/{*}value', 'get("use")'),
            'pcp_id_root': ('.//{*}principalCareProviderId/{*}value', 'get("root")'),
            'pcp_id_extension': ('.//{*}principalCareProviderId/{*}value', 'get("extension")'),
        }
        # create a dictionary to extract parameters. key: parameter name, value: path
        extracted_parameters = {}
        for key, value in params.items():
            children = root.find(value[0])
            if children is None:
                continue

            extracted_parameters[key] = eval("children."+value[1])
            if key == 'gender':
                extracted_parameters[key] = utils.gender_ambiguous_formatting(
                    extracted_parameters[key])
            elif key == 'birthtime':
                extracted_parameters[key] = utils.birthdate_ambiguous_formatting(
                    extracted_parameters[key])

        # LivingSubjectName Parameter (R)
        # LivingSubjectAdministrativeGender Parameter (O)
        # LivingSubjectBirthTime Parameter (R)
        # PatientAddress Parameter (O)
        # LivingSubjectId Parameter (O) ?       !CANNOT FIND!
        # LivingSubjectBirthPlaceAddress Parameter (O) !CANNOT FIND!
        # LivingSubjectBirthPlaceName Parameter (O) !CANNOT FIND!
        # MothersMaidenName Parameter (O)
        # PatientTelecom (O)
        # PrincipalCareProviderId (O) ?? NEED ONE OF THE TWO (use, value)

        # combine into dictionary to return
        self.extracted_parameters = extracted_parameters
        # TODO: some optional parameters are missing from gazelle_FULLRequest.xsd, but are implementable with our db
        return extracted_parameters

    def search_db(self):
        '''
        search our database for someone, or a set of people that fit the demographic parameters
        returns the search result as a dictionary of patients, where keys are pids or fhirids, whichever is more convenient, and values are ALL the
        demographic params and values in the 'params' dict, not restricted to only the ones queried;
            if we don't have certain params, value is '' (empty string)
            example dictionary: {'aehrlaeiuhr1218jeshfalhf':{'given':['Grace'],'family':'Zzambmaster','gender':'F', etc.}}
        if no results are found, returns an empty dictionary
        if multiple results are found, the dictionary should have multiple entries
        '''
        cq_metadata = self.extracted_parameters
        print("cq_metadata", cq_metadata)
        lambda_client = boto3.client('lambda')
        response = lambda_client.invoke(
            FunctionName=f"",
            InvocationType='RequestResponse',
            Payload=json.dumps({"body": json.dumps({
                "use_case": "find_pid_within_org_cq",
                "org_id": self.org_id,
                "cq_metadata": cq_metadata,
            })})
        )
        response_body_str = json.loads(response["Payload"].read().decode("utf-8"))["body"]
        print("response_body_str", response_body_str)
        org_pid = json.loads(response_body_str)["pid"]

        self.org_pid = org_pid
        return org_pid

    def get_patient_metadata(self):
        '''
        TODO:get patient metadata from database with pid
        currently use what's in the query directly
        '''
        # for id in final_ids:
        #     known_facts = {}
        #     query = f"SELECT resource FROM Patient WHERE id = '{id}'"
        #     self.cur.execute(query)
        #     resource = self.cur.fetchone()[0]
        #     known_facts['given'] = self.get_given_name_from_resource(resource)
        #     known_facts['family'] = self.get_family_name_from_resource(resource)
        #     known_facts['gender'] = self.get_gender_from_resource(resource)
        #     known_facts['birthtime'] = self.get_birthdate_from_resource(resource)
        #     known_facts['line'] = self.get_line_from_resource(resource)
        #     known_facts['city'] = self.get_city_from_resource(resource)
        #     known_facts['country'] = self.get_country_from_resource(resource)
        #     known_facts['postal_code'] = self.get_postal_code_from_resource(resource)
        #     known_facts['pcp_extension'] = self.get_pcp_extension_from_resource(resource)
        #     known_facts['pcp_root'] = self.get_pcp_root_from_resource(resource)
        #     known_facts['mothers_maiden_name'] = self.get_mothers_maiden_name_from_resource(
        #         resource)
        #     known_facts['telephone'] = self.get_telephone_from_resource(resource)
        #     known_facts['telecom_use'] = self.get_telecom_use_from_resource(resource)
        #     patients_dict[id] = known_facts

        # self.search_result = patients_dict
        # print("patients_dict:", patients_dict)

        # return patients_dict
        if self.org_pid:
            patients_dict = {self.org_pid: self.extracted_parameters}
            self.search_result = patients_dict
            return patients_dict

    # change to be able to take in multiple values for the same field
    def create_fill_content_dict(self, pid):
        print("self.search_result,", self.search_result)
        fill_content = {
            'pid': pid, 'given': self.search_result[pid].get('given', 'None'),
            'family': self.search_result[pid].get('family', 'None'),
            'birthTime': self.search_result[pid].get('birthtime', 'None'),
            'genderCode': self.search_result[pid].get('gender', 'None'),
            'genderDisplay': 'None', 'tel': self.search_result[pid].get('telephone', 'None'),
            'telecomUse': self.search_result[pid].get('telecom_use', 'None'),
            'streetAddressLine': self.search_result[pid].get('line', 'None'),
            'city': self.search_result[pid].get('city', 'None'),
            'country': self.search_result[pid].get('country', 'None'),
            'postalCode': self.search_result[pid].get('postal_code', 'None'),
            'pcpExt': self.search_result[pid].get('pcp_extension', 'None'),
            'pcpRoot': self.search_result[pid].get('pcp_root', 'None'),
            'mmName': self.search_result[pid].get('mmName', 'None'),
            'ourHCID': self.org_hcid, 'theirHCID': self.receiver_hcid, 'ourURL': self.url,
            'creationTime': datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'),
            'orgName': self.org_name, 'ourWebsite': '',
            'query_id_element': etree.tostring(self.query_id_element, encoding='unicode'),
            'query_by_parameter_element': etree.tostring(
                self.query_by_parameter_element, encoding='unicode'), }
        return fill_content

    def create_fill_content_dict_nf(self):
        return {"query_id_element": etree.tostring(self.query_id_element, encoding='unicode'),
                "query_by_parameter_element": etree.tostring(self.query_by_parameter_element, encoding='unicode'),
                "theirHCID": self.receiver_hcid,
                # use org_hcid so that we know which database to look in when we get iti38 and iti39 requests
                "ourHCID": self.org_hcid,
                "ourURL": self.url,
                "creationTime": datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'),
                }

    def fill_response(self):
        '''
        compiles the search_db results into the desired xml format according to ITI-55 specification
        sends the response to the initiating gateway who made the request
        you should be able to convey any of the four cases
            we have found multiple matches, return nothing
                TODO: optionally, request more demographic information to narrow down the patient
            we have found a single match, return all we stored about this single patient
                TODO: we're missing some parameters in our returned response which we should add, though they're not in the examples
            we have found no match
            your request has caused the following error, “error message”
        '''
        # TODO: if we want the contact info of each org to be org specific,
        # we need to add a field to the org table to store the contact info and replace ourWebsite
        try:
            assert (len(self.search_result) == 1)
            # single match
            response_template = \
                '''
            <PRPA_IN201306UV02 ITSVersion="XML_1.0" xmlns="urn:hl7-org:v3">
                <id extension="0000" root="{ourHCID}"/>
                <creationTime value="{creationTime}"/>
                <interactionId extension="PRPA_IN201306UV02" root="{ourHCID}"/>
                <processingCode code="T"/>
                <processingModeCode code="T"/>
                <acceptAckCode code="NE"/>
                <receiver typeCode="RCV">
                    <device classCode="DEV" determinerCode="INSTANCE">
                        <id root="{theirHCID}"/>
                    </device>
                </receiver>
                <sender typeCode="SND">
                    <device classCode="DEV" determinerCode="INSTANCE">
                        <id root="{ourHCID}"/>
                        <telecom value="{ourURL}"/>
                    </device>
                </sender>
                <acknowledgement>
                    <typeCode code="AA"/>
                    <targetMessage>
                        <id extension="0000" root="1.3.6.1.4.1.12559.11.1.2.2.5.10.1"/>
                    </targetMessage>
                </acknowledgement>
                <controlActProcess classCode="CACT" moodCode="EVN">
                    <code code="PRPA_TE201306UV02" displayName="2.16.840.1.113883.1.18"/>
                    <subject contextConductionInd="false" typeCode="SUBJ">
                        <registrationEvent classCode="REG" moodCode="EVN">
                            <statusCode code="active"/>
                            <subject1 typeCode="SBJ">
                                <patient classCode="PAT">
                                    <id extension="{pid}" root="{ourHCID}"/>
                                    <statusCode code="active"/>
                                    <patientPerson classCode="PSN" determinerCode="INSTANCE">
                                        <name>
                                            <given>{given}</given>
                                            <family>{family}</family>
                                        </name>
                                        <administrativeGenderCode code="{genderCode}" codeSystem="2.16.840.1.113883.12.1" displayName="{genderDisplay}"/>
                                        <birthTime value="{birthTime}"/>
                                        <telecom value="tel:{tel}" use="{telecomUse}"/>
                                        <addr>
                                            <streetAddressLine>{streetAddressLine}</streetAddressLine>
                                            <city>{city}</city>
                                            <country>{country}</country>
                                            <postalCode>{postalCode}</postalCode>
                                        </addr>
                                        <principalCareProviderId>
                                            <value extension="{pcpExt}" root="{pcpRoot}"/>
                                            <semanticsText>AssignedProvider.id</semanticsText>
                                        </principalCareProviderId>
                                        <mothersMaidenName>
                                            <value>
                                                <family>{mmName}</family>
                                            </value>
                                            <semanticsText>Person.MothersMaidenName</semanticsText>
                                        </mothersMaidenName>
                                    </patientPerson>
                                    <providerOrganization classCode="ORG" determinerCode="INSTANCE">
                                        <id root="{ourHCID}"/>
                                        <name>"{orgName}"</name>
                                        <contactParty classCode="CON">
                                            <id root="{ourHCID}"/>
                                            <telecom value="{ourWebsite}"/>
                                        </contactParty>
                                    </providerOrganization>
                                    <subjectOf1>
                                        <queryMatchObservation classCode="COND" moodCode="EVN">
                                            <code code="IHE_PDQ"/>
                                            <value xsi:type="INT" value="100" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>
                                        </queryMatchObservation>
                                    </subjectOf1>
                                </patient>
                            </subject1>
                            <custodian typeCode="CST">
                                <assignedEntity classCode="ASSIGNED">
                                    <id root="{ourHCID}"/>
                                    <code code="NotHealthDataLocator" codeSystem="1.3.6.1.4.1.19376.1.2.27.2"/>
                                </assignedEntity>
                            </custodian>
                        </registrationEvent>
                    </subject>
                    <queryAck>
                        {query_id_element}
                        <statusCode code="deliveredResponse"/>
                        <queryResponseCode code="OK"/>
                    </queryAck>
                    {query_by_parameter_element}
                </controlActProcess>
            </PRPA_IN201306UV02>
            '''
            pid = next(iter(self.search_result))  # only take first patient
            print("search result pid,", pid)
            fill_content = self.create_fill_content_dict(pid)
            print("created fill content dict for found patient")

        except:  # too few or too many matches, return nothing
            # create a response template for no return and directly import and use it
            response_template = \
                '''
            <PRPA_IN201306UV02 ITSVersion="XML_1.0" xmlns="urn:hl7-org:v3">
                <id extension="0000" root="{ourHCID}"/>
                <creationTime value="{creationTime}"/>
                <interactionId extension="PRPA_IN201306UV02" root="{ourHCID}"/>
                <processingCode code="T"/>
                <processingModeCode code="T"/>
                <acceptAckCode code="NE"/>
                <receiver typeCode="RCV">
                    <device classCode="DEV" determinerCode="INSTANCE">
                        <id root="{theirHCID}"/>
                    </device>
                </receiver>
                <sender typeCode="SND">
                    <device classCode="DEV" determinerCode="INSTANCE">
                        <id root="{ourHCID}"/>
                        <telecom value="{ourURL}"/>
                    </device>
                </sender>
                <acknowledgement>
                    <typeCode code="AA"/>
                    <targetMessage>
                        <id extension="0000" root="1.3.6.1.4.1.12559.11.1.2.2.5.10.1"/>
                    </targetMessage>
                </acknowledgement>
                <controlActProcess classCode="CACT" moodCode="EVN">
                        <code code="PRPA_TE201306UV02" displayName="2.16.840.1.113883.1.18"/>
                    <queryAck>
                        {query_id_element}
                        <statusCode code="deliveredResponse"/>
                        <queryResponseCode code="NF"/>
                    </queryAck>
                    {query_by_parameter_element}
                </controlActProcess>
            </PRPA_IN201306UV02>'''

            fill_content = self.create_fill_content_dict_nf()

        formatted_response = response_template.format(**fill_content)
        self.response_body = etree.fromstring(formatted_response)
        return self.response_body

    def generate_response_body(self):
        '''
        generates the response body from the search_db results
        '''
        self.write_request()
        self.process_xcpd_request()

        # if the org_hcid is valid and can be found in the database
        # search db to find the patient ids that match the demographic parameters
        # if self.org_hcid:
        #     print("org_hcid:", self.org_hcid)
        #     self.org_id, self.org_name = utils.get_org_id_and_name_from_org_hcid(self.org_hcid)
        #     if self.org_id:
        #         conn = utils.get_db_connection(utils.get_org_db_name_from_org_id(self.org_id))
        #         self.cur = conn.cursor()
        #         self.search_db()
        #         self.get_patient_metadata()
        #         self.cur.close()
        #         conn.commit()
        #         conn.close()

        self.fill_response()
        return self.response_body
