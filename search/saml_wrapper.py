import base64
import boto3
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import OpenSSL.crypto
import signxml
from lxml import etree
from lxml.builder import ElementMaker
from lxml.etree import QName
from saml2 import extension_element_from_string
from saml2.saml import (NAMEID_FORMAT_ENTITY, NAMEID_FORMAT_UNSPECIFIED,
                        NAMEID_FORMAT_X509SUBJECTNAME, SCM_HOLDER_OF_KEY,
                        Assertion, Attribute, AttributeStatement,
                        AttributeValue, Audience, AudienceRestriction,
                        AuthnContext, AuthnContextClassRef, AuthnStatement,
                        Conditions, Issuer, KeyInfoConfirmationDataType_,
                        NameID, Subject, SubjectConfirmation,
                        SubjectConfirmationData)
from saml_config import *
from signxml import DigestAlgorithm, SignatureMethod, XMLSigner, XMLVerifier

secretsmanager = boto3.client('secretsmanager')
secret_id = ""
secret_params = {}
cq_cert = ''
cq_private_key = ''


class Saml(object):

    def __init__(self):
        self.issued_at = datetime.now(timezone.utc)

    def __new__(cls):
        if not hasattr(cls, 'instance'):
            cls.instance = super(Saml, cls).__new__(cls)
            # load all the key and cert files

            cls.instance.key = bytes(cq_private_key, 'raw-unicode-escape')
            cls.instance.cert = bytes(cq_cert, 'raw-unicode-escape')

        return cls.instance

    def create_saml_assertion_string(self, audience, role, purposeOfUse, user_qualifications):
        subject_name = user_qualifications['subject_name']
        organization = user_qualifications['organization']
        npi = user_qualifications['npi']
        org_hcid = user_qualifications['org_hcid']

        ### Create the SAML assertion ###
        # https://www.hl7.org/fhir/codesystem-nhin-purposeofuse.html
        issuer = ISSUER
        not_on_or_after = self.issued_at + timedelta(hours=1)
        refID = str(uuid.uuid4())

        # Create SAML assertion
        issuer = Issuer(
            name_qualifier=NAMEID_FORMAT_X509SUBJECTNAME,
            format=NAMEID_FORMAT_X509SUBJECTNAME,
            text=CERT_SUBJECT
        )
        subjectConfirmationData = SubjectConfirmationData()
        subject = Subject(
            name_id=NameID(
                format=NAMEID_FORMAT_X509SUBJECTNAME,
                text=CERT_SUBJECT
            ),
            subject_confirmation=SubjectConfirmation(
                method=SCM_HOLDER_OF_KEY,
                subject_confirmation_data=subjectConfirmationData
            )
        )

        nsmap = {
            "ns2": "urn:hl7-org:v3",
            "xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsd": "http://www.w3.org/2001/XMLSchema",
        }

        p_E = ElementMaker(namespace=nsmap["ns2"], nsmap=nsmap)
        pou_element = p_E.PurposeOfUse(
            {QName("urn:hl7-org:v3", "type"): "CE"},
            code=purposeOfUse,
            codeSystem="2.16.840.1.113883.3.18.7.1",
            codeSystemName="nhin-purpose",
            displayName=purposeOfUse
        )

        r_E = ElementMaker(namespace=nsmap["ns2"], nsmap=nsmap)
        role_element = r_E.Role(
            {QName("urn:hl7-org:v3", "type"): "CE"},
            code=role,
            codeSystem="2.16.840.1.113883.6.96",
            codeSystemName="SNOMED_CT",
            displayName=""
        )

        extension_pou = extension_element_from_string(etree.tostring(pou_element))

        extension_role = extension_element_from_string(etree.tostring(role_element))

        # Create the attribute statement
        attributes = [
            Attribute(
                name="urn:oasis:names:tc:xspa:1.0:subject:subject-id",
                friendly_name="XSPA Subject",
                attribute_value=AttributeValue(subject_name)
            ),
            Attribute(
                name="urn:oasis:names:tc:xspa:1.0:subject:organization",
                attribute_value=AttributeValue(organization)
            ),
            Attribute(
                name="urn:oasis:names:tc:xspa:2.0:subject:npi",
                friendly_name="NPI",
                attribute_value=AttributeValue(npi)
            ),
            Attribute(
                name="urn:oasis:names:tc:xspa:1.0:subject:organization-id",
                friendly_name="XSPA Organization ID",
                attribute_value=AttributeValue("urn:oid:"+org_hcid)
            ),
            Attribute(
                name="urn:nhin:names:saml:homeCommunityId",
                friendly_name="XCA Home Community ID",
                attribute_value=AttributeValue("urn:oid:"+org_hcid)
            ),
            Attribute(
                name="urn:oasis:names:tc:xspa:1.0:subject:purposeofuse",
                friendly_name="Purpose of Use",
                attribute_value=AttributeValue(extension_elements=[extension_pou])
            ),
            Attribute(
                name="urn:oasis:names:tc:xacml:2.0:subject:role",
                friendly_name="HL7 Role",
                attribute_value=AttributeValue(extension_elements=[extension_role])
            )
        ]

        attribute_statement = AttributeStatement(attribute=attributes)
        conditions = Conditions(
            not_before=self.issued_at.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            not_on_or_after=not_on_or_after.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            audience_restriction=AudienceRestriction([
                Audience(audience)
            ])
        )

        authn_statement = AuthnStatement(
            authn_instant=self.issued_at.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            authn_context=AuthnContext(
                authn_context_class_ref=AuthnContextClassRef(
                    text="urn:oasis:names:tc:SAML:2.0:ac:classes:Password"
                )
            )
        )

        assertion = Assertion(
            id="_"+refID,
            issue_instant=self.issued_at.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            issuer=issuer,
            subject=subject,
            conditions=conditions,
            attribute_statement=attribute_statement,
            version="2.0",
            authn_statement=authn_statement
        )

        assertion_string = str(assertion)

        assertion_string = assertion_string.replace(
            "Issuer>", "Issuer>" +
            "<ds:Signature xmlns:ds=\"http://www.w3.org/2000/09/xmldsig#\" Id=\"placeholder\"></ds:Signature>")
        assertion_string = assertion_string.replace("ns0", "samlns")
        assertion_string = assertion_string.replace(
            "<samlns:SubjectConfirmationData />",
            "<samlns:SubjectConfirmationData></samlns:SubjectConfirmationData>")
        saml_root = etree.fromstring(assertion_string)

        sub_tag = saml_root.xpath(
            "/samlns:Assertion/samlns:Subject/samlns:SubjectConfirmation/samlns:SubjectConfirmationData",
            namespaces={'samlns': 'urn:oasis:names:tc:SAML:2.0:assertion'})
        sub_tag[0].attrib[QName("urn:oasis:names:tc:SAML:2.0:assertion",
                                "type")] = "KeyInfoConfirmationDataType"

        # Create the KeyInfo element in Subject with RSAKeyValue
        key_map = {"dsig": "http://www.w3.org/2000/09/xmldsig#"}
        k_E = ElementMaker(namespace=key_map["dsig"], nsmap=key_map)
        modulus = k_E("Modulus")
        key = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM, cq_private_key)
        new_modulus = key.to_cryptography_key().public_key().public_numbers().n
        # needs it to be hex and then base64
        r_key = str(
            base64.b64encode(
                int(new_modulus).to_bytes(
                    (int(new_modulus).bit_length() + 7) // 8,
                    byteorder='big')
            ),
            'utf-8'
        )
        modulus.text = r_key
        exponent = k_E("Exponent")
        exponent.text = "AQAB"
        rsa_element = k_E.RSAKeyValue(modulus, exponent)
        keyValue_element = k_E.KeyValue(rsa_element)
        keyInfo_element = k_E.KeyInfo(keyValue_element)
        sub_tag[0].insert(0, keyInfo_element)

        signed_saml_root = XMLSigner(
            method=signxml.methods.enveloped, c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
            signature_algorithm=SignatureMethod.RSA_SHA1, digest_algorithm=DigestAlgorithm.SHA1).sign(
            saml_root, key=self.key, cert=self.cert, always_add_key_value=True)
        verified_data = XMLVerifier().verify(signed_saml_root, x509_cert=self.cert).signed_xml

        securityHeader = etree.fromstring(
            "<wsse:Security soapenv:mustUnderstand=\"true\" xmlns:soapenv=\"http://www.w3.org/2003/05/soap-envelope\" xmlns:wsu=\"http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd\" xmlns:wsse=\"http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd\"></wsse:Security>")
        securityHeader.append(signed_saml_root)
        return securityHeader, refID

    def sign_soap_message(self, soap_message, refID, own_url, destination_url):

        etree.register_namespace("a", 'http://www.w3.org/2005/08/addressing')
        etree.register_namespace("soap-env", "http://www.w3.org/2003/05/soap-envelope")
        etree.register_namespace("ds", "http://www.w3.org/2000/09/xmldsig#")
        etree.register_namespace(
            "wsu",
            "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd")
        etree.register_namespace(
            "wsse",
            "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd")

        soap_etree = etree.fromstring(soap_message)

        header = soap_etree[0]

        securityTag = header[0]

        toTag = header[3]

        toTag.attrib[QName(
            "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd", "Id")] = "_1"
        toTag.text = destination_url

        # Add message Id, ReplyTo, To and From headers
        messageId = "urn:uuid:" + str(uuid.uuid4())
        actionElement = etree.fromstring(
            f'<a:Action xmlns:a="http://www.w3.org/2005/08/addressing" xmlns:soap-env="http://www.w3.org/2003/05/soap-envelope" soap-env:mustUnderstand="1">urn:hl7-org:v3:PRPA_IN201305UV02:CrossGatewayPatientDiscovery</a:Action>')
        messageElement = etree.fromstring(
            f'<a:MessageID xmlns:a="http://www.w3.org/2005/08/addressing">{messageId}</a:MessageID>')

        # Create the timestamp element
        time_map = {
            "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"}
        time_created = self.issued_at.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        time_expires = (self.issued_at + timedelta(hours=1)
                        ).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

        t_E = ElementMaker(namespace=time_map["wsu"], nsmap=time_map)
        created = t_E("Created")
        created.text = time_created
        expires = t_E("Expires")
        expires.text = time_expires
        timestamp = t_E.Timestamp(created, expires, {QName(time_map["wsu"], "Id"): "_0"})
        securityTag.insert(0, timestamp)

        # Create the SecurityTokenReference element
        nsmap = {"wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
                 "dsig": "http://www.w3.org/2000/09/xmldsig#",
                 "b": "http://docs.oasis-open.org/wss/oasis-wss-wssecurity-secext-1.1.xsd",
                 }
        E = ElementMaker(namespace=nsmap["wsse"], nsmap=nsmap)
        key_iden_ref = E(
            "KeyIdentifier",
            ValueType="http://docs.oasis-open.org/wss/oasis-wss-saml-token-profile-1.1#SAMLID")
        key_iden_ref.text = "_"+refID

        s_E = ElementMaker(namespace=nsmap["wsse"], nsmap=nsmap)
        security_token_reference = s_E.SecurityTokenReference(key_iden_ref, {QName(
            nsmap["b"], "TokenType"): "http://docs.oasis-open.org/wss/oasis-wss-saml-token-profile-1.1#SAMLV2.0"})

        no_nsE = ElementMaker(namespace=nsmap["dsig"], nsmap=nsmap)
        key_info = no_nsE.KeyInfo(security_token_reference)

        # Sign the timestamp
        signedXml = XMLSigner(
            method=signxml.methods.detached, c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
            signature_algorithm=SignatureMethod.RSA_SHA1, digest_algorithm=DigestAlgorithm.SHA1).sign(
            soap_etree, key=self.key, cert=self.cert, key_info=key_info, reference_uri=["#_0", "#_1"],
            always_add_key_value=False)

        # not sure if working:
        # verified_data = XMLVerifier().verify(signedXml, x509_cert=self.cert).signed_xml

        securityTag.append(signedXml)

        # move the security tag to the end
        header.append(securityTag)

        return etree.tostring(soap_etree, encoding="UTF-8", xml_declaration=False)
