# SCIM 2.0 Discovery Constants
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

SERVICE_PROVIDER_CONFIG = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
    "documentationUri": "https://docs.bagofwords.com/enterprise/scim",
    "patch": {"supported": True},
    "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
    "filter": {"supported": True, "maxResults": 100},
    "changePassword": {"supported": False},
    "sort": {"supported": False},
    "etag": {"supported": False},
    "authenticationSchemes": [
        {
            "type": "oauthbearertoken",
            "name": "OAuth Bearer Token",
            "description": "Authentication scheme using the OAuth Bearer Token Standard",
            "specUri": "https://www.rfc-editor.org/info/rfc6750",
            "primary": True,
        }
    ],
}

SCHEMAS = [
    {
        "id": "urn:ietf:params:scim:schemas:core:2.0:User",
        "name": "User",
        "description": "User Account",
        "attributes": [
            {
                "name": "userName",
                "type": "string",
                "multiValued": False,
                "required": True,
                "caseExact": False,
                "mutability": "readWrite",
                "returned": "default",
                "uniqueness": "server",
            },
            {
                "name": "name",
                "type": "complex",
                "multiValued": False,
                "required": False,
                "mutability": "readWrite",
                "returned": "default",
                "subAttributes": [
                    {"name": "formatted", "type": "string", "multiValued": False, "required": False, "mutability": "readWrite", "returned": "default"},
                    {"name": "givenName", "type": "string", "multiValued": False, "required": False, "mutability": "readWrite", "returned": "default"},
                    {"name": "familyName", "type": "string", "multiValued": False, "required": False, "mutability": "readWrite", "returned": "default"},
                ],
            },
            {
                "name": "displayName",
                "type": "string",
                "multiValued": False,
                "required": False,
                "mutability": "readWrite",
                "returned": "default",
            },
            {
                "name": "emails",
                "type": "complex",
                "multiValued": True,
                "required": False,
                "mutability": "readWrite",
                "returned": "default",
                "subAttributes": [
                    {"name": "value", "type": "string", "multiValued": False, "required": True, "mutability": "readWrite", "returned": "default"},
                    {"name": "type", "type": "string", "multiValued": False, "required": False, "mutability": "readWrite", "returned": "default"},
                    {"name": "primary", "type": "boolean", "multiValued": False, "required": False, "mutability": "readWrite", "returned": "default"},
                ],
            },
            {
                "name": "active",
                "type": "boolean",
                "multiValued": False,
                "required": False,
                "mutability": "readWrite",
                "returned": "default",
            },
            {
                "name": "externalId",
                "type": "string",
                "multiValued": False,
                "required": False,
                "mutability": "readWrite",
                "returned": "default",
            },
        ],
        "meta": {
            "resourceType": "Schema",
            "location": "/scim/v2/Schemas/urn:ietf:params:scim:schemas:core:2.0:User",
        },
    }
]

RESOURCE_TYPES = [
    {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/scim/v2/Users",
        "description": "User Account",
        "schema": "urn:ietf:params:scim:schemas:core:2.0:User",
        "schemaExtensions": [],
        "meta": {
            "resourceType": "ResourceType",
            "location": "/scim/v2/ResourceTypes/User",
        },
    }
]
