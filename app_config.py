import os

b2c_tenant = "zispireplatform"
signupsignin_user_flow = "B2C_1_susi"
editprofile_user_flow = "B2C_1_profile"

# resetpassword_user_flow = "B2C_1_passwordreset1"  # Note: Legacy setting.

authority_template = "https://{tenant}.b2clogin.com/{tenant}.onmicrosoft.com/{user_flow}"

CLIENT_ID = "364fa50d-061f-434e-ba9c-35f5cf8b69df" # Application (client) ID of app registration

CLIENT_SECRET = "Y.w8Q~YcysFhAOCiF4jOJu7s4mr.F5KXHy7wAc4-" # Application secret.

AUTHORITY = authority_template.format(
    tenant=b2c_tenant, user_flow=signupsignin_user_flow)
B2C_PROFILE_AUTHORITY = authority_template.format(
    tenant=b2c_tenant, user_flow=editprofile_user_flow)

# B2C_RESET_PASSWORD_AUTHORITY = authority_template.format(tenant=b2c_tenant, user_flow=resetpassword_user_flow)

REDIRECT_PATH = "/getAToken"  

# This is the API resource endpoint
ENDPOINT = '' # Application ID URI of app registration in Azure portal

# These are the scopes you've exposed in the web API app registration in the Azure portal
SCOPE = []  # Example with two exposed scopes: ["demo.read", "demo.write"]

SESSION_TYPE = "filesystem"  # Specifies the token cache should be stored in server-side session