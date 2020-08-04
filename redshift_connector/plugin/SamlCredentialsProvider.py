import base64
import logging
import random
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import boto3
import bs4

from redshift_connector.CredentialsHolder import CredentialsHolder
from redshift_connector.error import InterfaceError
from redshift_connector.RedshiftProperty import RedshiftProperty

logger = logging.getLogger(__name__)


class SamlCredentialsProvider(ABC):
    def __init__(self) -> None:
        self.user_name: Optional[str] = None
        self.password: Optional[str] = None
        self.idp_host: Optional[str] = None
        self.idpPort: int = 443
        self.duration: Optional[int] = None
        self.preferred_role: Optional[str] = None
        self.sslInsecure: Optional[bool] = None
        self.db_user: Optional[str] = None
        self.db_groups: Optional[List[str]] = None
        self.force_lowercase: Optional[bool] = None
        self.auto_create: Optional[bool] = None
        self.region: Optional[str] = None
        self.principal: Optional[str] = None

        self.cache: dict = {}

    def add_parameter(self, info: RedshiftProperty) -> None:
        self.user_name = info.user_name
        self.password = info.password
        self.idp_host = info.idp_host
        self.idpPort = info.idpPort
        self.duration = info.duration
        self.preferred_role = info.preferred_role
        self.sslInsecure = info.sslInsecure
        self.db_user = info.db_user
        self.db_groups = info.db_groups
        self.force_lowercase = info.force_lowercase
        self.auto_create = info.auto_create
        self.region = info.region
        self.principal = info.principal

    def get_credentials(self) -> CredentialsHolder:
        key: str = self.get_cache_key()
        if key not in self.cache or self.cache[key].is_expired():
            try:
                self.refresh()
            except Exception as e:
                logger.error("refresh failed: {}".format(str(e)))
                raise InterfaceError(e)
        # if the SAML response has db_user argument, it will be picked up at this point.
        credentials: CredentialsHolder = self.cache[key]

        if credentials is None:
            raise InterfaceError("Unable to load AWS credentials from IDP")

        # if db_user argument has been passed in the connection string, add it to metadata.
        if self.db_user:
            credentials.metadata.set_db_user(self.db_user)

        return credentials

    def refresh(self) -> None:
        try:
            # get SAML assertion from specific identity provider
            saml_assertion = self.get_saml_assertion()
        except Exception as e:
            logger.error("get saml assertion failed: {}".format(str(e)))
            raise InterfaceError(e)
        # decode SAML assertion into xml format
        doc: bytes = base64.b64decode(saml_assertion)

        soup = bs4.BeautifulSoup(doc,'xml')
        attrs = soup.findAll('Attribute')
        # extract RoleArn adn PrincipleArn from SAML assertion
        role_pattern = re.compile(r'arn:aws:iam::\d*:role/\S+')
        provider_pattern = re.compile(r'arn:aws:iam::\d*:saml-provider/\S+')
        roles: Dict[str, str] = {}
        for attr in attrs:
            name: str = attr.attrs['Name']
            values: Any = attr.findAll('AttributeValue')
            if name == "https://aws.amazon.com/SAML/Attributes/Role":
                for value in values:
                    arns = value.contents[0].split(',')
                    role: str = ''
                    provider: str = ''
                    for arn in arns:
                        if role_pattern.match(arn):
                            role = arn
                        if provider_pattern.match(arn):
                            provider = arn
                    if role != '' and provider != '':
                        roles[role] = provider

        if len(roles) == 0:
            raise InterfaceError("No role found in SamlAssertion")
        role_arn: str = ''
        principle: str = ''
        if self.preferred_role:
            role_arn = self.preferred_role
            if role_arn not in roles:
                raise InterfaceError("Preferred role not found in SamlAssertion")
            principle = roles[role_arn]
        else:
            role_arn = random.choice(list(roles))
            principle = roles[role_arn]

        client = boto3.client('sts')

        try:
            response = client.assume_role_with_saml(
                RoleArn=role_arn,   # self.preferred_role,
                PrincipalArn=principle,   # self.principal,
                SAMLAssertion=saml_assertion
            )

            stscred: Dict[str, Any] = response['Credentials']
            credentials: CredentialsHolder = CredentialsHolder(stscred)
            # get metadata from SAML assertion
            credentials.set_metadata(self.read_metadata(doc))
            key: str = self.get_cache_key()
            self.cache[key] = credentials
        except AttributeError as e:
            logger.error("AttributeError: %s", e)
            raise e
        except KeyError as e:
            logger.error("KeyError: %s", e)
            raise e
        except client.exceptions.MalformedPolicyDocumentException as e:
            logger.error("MalformedPolicyDocumentException: %s", e)
            raise e
        except client.exceptions.PackedPolicyTooLargeException as e:
            logger.error("PackedPolicyTooLargeException: %s", e)
            raise e
        except client.exceptions.IDPRejectedClaimException as e:
            logger.error("IDPRejectedClaimException: %s", e)
            raise e
        except client.exceptions.InvalidIdentityTokenException as e:
            logger.error("InvalidIdentityTokenException: %s", e)
            raise e
        except client.exceptions.ExpiredTokenException as e:
            logger.error("ExpiredTokenException: %s", e)
            raise e
        except client.exceptions.RegionDisabledException as e:
            logger.error("RegionDisabledException: %s", e)
            raise e
        except Exception as e:
            logger.error("other Exception: %s", e)
            raise e

    def get_cache_key(self) -> str:
        return '{username}{password}{idp_host}{idp_port}{duration}{preferred_role}'.format(
            username=self.user_name, password=self.password, idp_host=self.idp_host, idp_port=self.idpPort,
            duration=self.duration, preferred_role=self.preferred_role)

    @abstractmethod
    def get_saml_assertion(self):
        pass

    def check_required_parameters(self) -> None:
        if self.user_name == '' or self.user_name is None:
            raise InterfaceError("Missing required property: user_name")
        if self.password == '' or self.password is None:
            raise InterfaceError("Missing required property: password")
        if self.idp_host == '' or self.idp_host is None:
            raise InterfaceError("Missing required property: idp_host")

    def read_metadata(self, doc: bytes) -> CredentialsHolder.IamMetadata:
        try:
            soup = bs4.BeautifulSoup(doc, 'xml')
            attrs = soup.findAll('saml2:Attribute')

            metadata: CredentialsHolder.IamMetadata = CredentialsHolder.IamMetadata()

            for attr in attrs:
                name: str = attr.attrs['Name']
                values: Any = attr.findAll('saml2:AttributeValue')  # [0].contents[0]
                if len(values) == 0:
                    # Ignore empty-valued attributes.
                    continue
                value: str = values[0].contents[0]

                if name == "https://redshift.amazon.com/SAML/Attributes/AllowDbUserOverride":
                    metadata.set_allow_db_user_override(value)
                elif name == "https://redshift.amazon.com/SAML/Attributes/DbUser":
                    metadata.set_saml_db_user(value)
                elif name == "https://aws.amazon.com/SAML/Attributes/RoleSessionName":
                    if metadata.get_saml_db_user() is None:
                        metadata.set_saml_db_user(value)
                elif name == "https://redshift.amazon.com/SAML/Attributes/AutoCreate":
                    metadata.set_auto_create(value)
                elif name == "https://redshift.amazon.com/SAML/Attributes/DbGroups":
                    groups = ','.join([value.contents[0] for value in values])
                    metadata.set_db_groups(groups)
                elif name == "https://redshift.amazon.com/SAML/Attributes/ForceLowercase":
                    metadata.set_force_lowercase(value)

            return metadata
        except AttributeError as e:
            logger.error("AttributeError: %s", e)
            raise e
        except KeyError as e:
            logger.error("KeyError: %s", e)
            raise e

    def get_form_action(self, soup) -> Optional[str]:
        for inputtag in soup.find_all(re.compile('(FORM|form)')):
            action: str = inputtag.get('action')
            if action:
                return action
        return None

    def is_text(self, inputtag) -> bool:
        return 'text' == inputtag.get('type')

    def is_password(self, inputtag) -> bool:
        return 'password' == inputtag.get('type')