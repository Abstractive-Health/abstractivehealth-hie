import asyncio
import boto3
import os
import ssl
import traceback
import uuid

import aiohttp
from lxml import etree
from requests import Session
from saml_wrapper import *
from zeep import Client, Settings
from zeep.transports import Transport

ENV = os.environ.get("ENV")


class ITI39Initiator:
    def __init__(self,
                 cur=None,
                 response=None,
                 params=None,
                 responder_url=None,
                 responder_hcid=None,
                 user_qualifications=None):
        self.params = params
        self.cur = cur
        self.request = response
        self.hcid = ""
        self.receiver_hcid = responder_hcid
        # overwrite with replacement hcid
        if "replacement_hcid" in self.params:
            self.receiver_hcid = self.params["replacement_hcid"]
        self.url = ""
        self.setup_done = False
        self.user_qualifications = user_qualifications

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

        self.session = session
        transport = Transport(session=session)

        self.client = Client('wsdls/drive_ITI39_responder.wsdl',
                             settings=settings,
                             transport=transport)
        self.service = self.client.create_service(
            '{urn:ihe:iti:xds-b:2007}RespondingGatewayRetrieve_Binding_Soap12',
            self.responder_url
        )

    def setup(self):
        if not self.setup_done:
            async_ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            async_ssl_ctx.load_cert_chain('/tmp/cqcert.crt', '/tmp/cqkey.key')
            # enforce verification of the server certificate with trusted.pem
            async_ssl_ctx.load_verify_locations('/tmp/trusted.pem')
            async_conn = aiohttp.TCPConnector(ssl_context=async_ssl_ctx)

            async_session = aiohttp.ClientSession(
                connector=async_conn,
                timeout=aiohttp.ClientTimeout(total=60)
            )
            self.async_session = async_session

            # do not use an asyncClient for wsdls that have imports. zeep compatibility is bad
            # self.async_client = AsyncClient('wsdls/drive_ITI39_responder.wsdl', settings=async_settings, transport=async_transport, plugins=[self.async_history])
            # self.async_service = self.async_client.create_service('{urn:ihe:iti:xds-b:2007}RespondingGatewayQuery_Binding_Soap12',self.responder_url)
            self.setup_done = True

    async def send_request(self):
        try:
            self.setup()
            # Create the request object
            DocumentRequest = [
                {
                    "HomeCommunityId": "urn:oid:"+self.receiver_hcid,
                    "RepositoryUniqueId": pair['rid'],
                    "DocumentUniqueId": pair["doc_id"]
                } for pair in self.params['pid_and_doc_ids']
            ]

            # Add the SAML assertion to the request
            saml = Saml()
            saml_assertion, refId = saml.create_saml_assertion_string(
                "http://ihe.connectathon.XUA/X-ServiceProvider-IHE-Connectathon", "",
                "", self.user_qualifications)

            soap_message = self.client.create_message(self.service,
                                                      'RespondingGateway_CrossGatewayRetrieve',
                                                      DocumentRequest=DocumentRequest,
                                                      _soapheaders=[saml_assertion])
            signed_message = saml.sign_soap_message(
                etree.tostring(soap_message),
                refId,
                self.url,
                self.responder_url
            )

            # Get the binding object
            endpoint = self.responder_url

            # Send the request asynchronously
            headers = {
                'Accept-Encoding': 'gzip, deflate, br',
                'Content-Type': 'application/soap+xml'
            }
            async with self.async_session.post(endpoint, data=signed_message, headers=headers) as response:
                try:
                    response_text = await response.text()
                    self.response_xml = response_text
                    self.process_response()
                    print(f"processed 39 response for {endpoint}")
                except (aiohttp.ClientConnectionError, aiohttp.ClientResponseError, asyncio.exceptions.TimeoutError) as e:
                    print(repr(e))
                    self.response_xml = None
                except Exception as e:
                    print("cannot utf-8 decode. here's the weird bytes like object,",
                          await response.content.read())
                    self.response_xml = ''

            await self.async_session.close()
            return self.response_xml

        except Exception:
            # Handle SOAP faults or errors
            print(traceback.format_exc())
            return None

    def process_response(self):
        # insert processing
        return self.response_xml
