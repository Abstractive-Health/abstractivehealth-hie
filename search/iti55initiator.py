import asyncio
import boto3
import os
import ssl
import traceback
import uuid
from datetime import datetime

import aiohttp
from lxml import etree
from requests import Session
from saml_wrapper import *
from zeep import Client, Settings
from zeep.transports import Transport

ENV = os.environ.get("ENV")


class ITI55Initiator:
    def __init__(self,
                 cur=None,
                 response=None,
                 params=None,
                 responder_url=None,
                 responder_hcid=None,
                 user_qualifications=None,
                 national=False
                 ):
        self.params = params
        self.cur = cur
        self.request = response
        self.hcid = ""
        self.receiver_hcid = responder_hcid
        self.url = ""
        self.setup_done = False
        self.user_qualifications = user_qualifications
        self.national = national
        self.timeout = 45 if national else 60
        self.current_time = datetime.now().strftime("%Y%m%d%H%M%S")

        # did not receive an xml request, only got initiator url (usually only for testing)
        if responder_url:
            self.responder_url = responder_url
            self.request = None
        else:
            self.request = response
            root = self.request
            self.responder_url = root.find('.//{*}ReplyTo/{*}Address').text

        settings = Settings(strict=False, force_https=True)
        session = Session()
        session.cert = ('/tmp/cqcert.crt', '/tmp/cqkey.key')
        session.verify = "/tmp/trusted.pem"
        session.verify = False

        self.session = session
        transport = Transport(session=session)

        # set up the sync client strictly for composing the message. use an async session to actually send the message
        self.client = Client('wsdls/gazelle_ITI55_responder.wsdl',
                             settings=settings,
                             transport=transport)
        self.service = self.client.create_service(
            '{urn:ihe:iti:xcpd:2009}RespondingGateway_ServiceSoapBinding',
            self.responder_url)

    def setup(self):
        if not self.setup_done:
            async_ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            async_ssl_ctx.load_cert_chain('/tmp/cqcert.crt', '/tmp/cqkey.key')
            # enforce verification of the server certificate with trusted.pem
            async_ssl_ctx.load_verify_locations('/tmp/trusted.pem')
            async_conn = aiohttp.TCPConnector(ssl_context=async_ssl_ctx)

            async_session = aiohttp.ClientSession(
                connector=async_conn, timeout=aiohttp.ClientTimeout(total=self.timeout))
            self.async_session = async_session

            # do not use an asyncClient for wsdls that have imports. zeep compatibility is bad
            self.setup_done = True

    async def send_request(self):
        try:
            self.setup()
            # Create the request object
            request_type = self.client.get_type("ns1:PRPA_IN201305UV02_._type")
            request = request_type(
                id={
                    "extension": "2211",
                    "root": str(uuid.uuid4())
                },
                creationTime={
                    "value": self.current_time
                },
                interactionId={
                    "extension": "PRPA_IN201305UV02",
                    "root": "2.16.840.1.113883.1.6"
                },
                processingCode={
                    "code": "P"
                },
                processingModeCode={
                    "code": "T"
                },
                acceptAckCode={
                    "code": "AL"
                },
                receiver={
                    "device": {
                        "classCode": "DEV",
                        "determinerCode": "INSTANCE",
                        "id": {
                            "root": self.receiver_hcid,
                        },
                        "asAgent": {
                            "classCode": "AGNT",
                            "representedOrganization": {
                                "classCode": "ORG",
                                "determinerCode": "INSTANCE",
                                "id": {
                                    "root": self.receiver_hcid,
                                }
                            }
                        }
                    },
                    "typeCode": "RCV"
                },
                sender={
                    "device": {
                        "classCode": "DEV",
                        "determinerCode": "INSTANCE",
                        "id": {
                            "root": self.hcid,
                        },
                        "asAgent": {
                            "classCode": "AGNT",
                            "representedOrganization": {
                                "classCode": "ORG",
                                "determinerCode": "INSTANCE",
                                "id": {
                                    "root": self.user_qualifications["org_hcid"]
                                }
                            }
                        }
                    },
                    "typeCode": "SND"
                },
                controlActProcess={
                    "code": {
                        "code": "PRPA_TE201305UV02",
                        "codeSystemName": "2.16.840.1.113883.1.6"
                    },
                    "authorOrPerformer": {
                        "assignedPerson": {
                            "classCode": "ASSIGNED"
                        },
                        "typeCode": "AUT"
                    },
                    "queryByParameter": {
                        "queryId": {
                            "root": "61023518-3f6e-4ad5-a465-87082e96b66f",
                        },
                        "statusCode": {
                            "code": "new"
                        },
                        "responseModalityCode": {
                            "code": "R"
                        },
                        "responsePriorityCode": {
                            "code": "I"
                        },
                        "matchCriterionList": {
                        },
                        "parameterList": {
                            "livingSubjectAdministrativeGender": {
                                "value": {
                                    "code": self.params['gender']
                                },
                                "semanticsText": "LivingSubject.AdministrativeGender"
                            },
                            "livingSubjectBirthTime": {
                                "value": {
                                    "value": self.params['date_of_birth']
                                },
                                "semanticsText": "LivingSubject.birthTime"
                            },
                            "livingSubjectName": {
                                "value": {
                                    "_value_1": [
                                        {
                                            "family": self.params['patient_family_name']
                                        },
                                        {
                                            "given": self.params['patient_given_name']
                                        }
                                    ]
                                },
                                "semanticsText": "LivingSubject.name"
                            }
                        }
                    },
                    "classCode": "CACT",
                    "moodCode": "EVN"
                },
                ITSVersion="XML_1.0"
            )

            if not self.national:  # consider if self.national
                request.controlActProcess["queryByParameter"]["parameterList"]["patientAddress"] = {
                    "value": {
                        "_value_1": []
                    },
                    "semanticsText": "Patient.addr"
                }
                if 'patient_address_street' in self.params and self.params['patient_address_street'] is not None:
                    request.controlActProcess["queryByParameter"]["parameterList"][
                        "patientAddress"]["value"]["_value_1"].append(
                        {"streetAddressLine": self.params['patient_address_street']})
                if 'patient_address_city' in self.params and self.params['patient_address_city'] is not None:
                    request.controlActProcess["queryByParameter"]["parameterList"][
                        "patientAddress"]["value"]["_value_1"].append(
                        {"city": self.params['patient_address_city']})
                if 'patient_address_state' in self.params and self.params['patient_address_state'] is not None:
                    request.controlActProcess["queryByParameter"]["parameterList"][
                        "patientAddress"]["value"]["_value_1"].append(
                        {"state": self.params['patient_address_state']})
                if 'patient_address_postal_code' in self.params and self.params[
                        'patient_address_postal_code'] is not None:
                    request.controlActProcess["queryByParameter"]["parameterList"][
                        "patientAddress"]["value"]["_value_1"].append(
                        {"postalCode": self.params['patient_address_postal_code']})
                if 'patient_address_country' in self.params and self.params['patient_address_country'] is not None:
                    request.controlActProcess["queryByParameter"]["parameterList"][
                        "patientAddress"]["value"]["_value_1"].append(
                        {"country": self.params['patient_address_country']})

                if len(request.
                        controlActProcess["queryByParameter"]["parameterList"]["patientAddress"]
                        ["value"]["_value_1"]) == 0:
                    del request.controlActProcess["queryByParameter"]["parameterList"][
                        "patientAddress"]

            if ('patient_phone' in self.params and self.params['patient_phone'] is not None) \
                    or ('patient_email' in self.params and self.params['patient_email'] is not None):
                request.controlActProcess["queryByParameter"]["parameterList"]["patientTelecom"] = {
                    "value": [],
                    "semanticsText": "Patient.telecom"
                }
                if 'patient_phone' in self.params and self.params['patient_phone'] is not None:
                    formatted_phone = self.params['patient_phone']
                    if len(formatted_phone) == 10:
                        formatted_phone = formatted_phone[:3]+"-" + \
                            formatted_phone[3:6]+"-"+formatted_phone[6:]
                    request.controlActProcess["queryByParameter"]["parameterList"][
                        "patientTelecom"]["value"].append(
                        {"value": "tel:+1-"+formatted_phone, "use": "HP", "_value_1": []}
                    )
                if 'patient_email' in self.params and self.params['patient_email'] is not None:
                    email = self.params['patient_email']
                    request.controlActProcess["queryByParameter"]["parameterList"][
                        "patientTelecom"]["value"].append(
                        {"value": "mailto:" + email, "use": "H", "_value_1": []}
                    )

            # Add the SAML assertion to the request headers
            saml = Saml()
            saml_assertion, refId = saml.create_saml_assertion_string(
                "http://ihe.connectathon.XUA/X-ServiceProvider-IHE-Connectathon",
                "insert role",
                "insert purpose of use",
                self.user_qualifications
            )

            # Send the request and get the response
            soap_message = self.client.create_message(
                self.service,
                'RespondingGateway_PRPA_IN201305UV02',
                id=request.id,
                creationTime=request.creationTime,
                interactionId=request.interactionId,
                processingCode=request.processingCode,
                processingModeCode=request.processingModeCode,
                acceptAckCode=request.acceptAckCode,
                receiver=request.receiver,
                sender=request.sender,
                controlActProcess=request.controlActProcess,
                ITSVersion="XML_1.0",
                _soapheaders=[saml_assertion])

            signed_message = saml.sign_soap_message(
                etree.tostring(soap_message),
                refId,
                self.url,
                self.responder_url
            )

            endpoint = self.responder_url

            # Send the request asynchronously
            headers = {
                'Accept-Encoding': 'gzip, deflate, br',
                'Content-Type': 'application/soap+xml'
            }
            async with self.async_session.post(endpoint, data=signed_message, headers=headers) as response:
                try:
                    response_text = await response.text()
                    print(f"got response from Endpoint, {endpoint}")
                    self.response_xml = response_text
                    self.process_response()
                except (aiohttp.ClientConnectionError, aiohttp.ClientResponseError, asyncio.exceptions.TimeoutError) as e:
                    print(repr(e))
                    self.response_xml = None
            await self.async_session.close()
            return self.response_xml

        except Exception:
            print(traceback.format_exc())
            return None

    def process_response(self):
        # insert processing
        return self.response_xml
