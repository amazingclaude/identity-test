import uuid
import requests
from flask import Flask, render_template, session, request, redirect, url_for
from flask_session import Session  # https://pythonhosted.org/Flask-Session
import msal
import app_config
import json


app = Flask(__name__)
app.config.from_object(app_config)
Session(app)

# This section is needed for url_for("foo", _external=True) to automatically
# generate http scheme when this sample is running on localhost,
# and to generate https scheme when it is deployed behind reversed proxy.
# See also https://flask.palletsprojects.com/en/1.0.x/deploying/wsgi-standalone/#proxy-setups
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


@app.route("/anonymous")
def anonymous():
    return "anonymous page"

@app.route("/")
def index():
    #if not session.get("user"):
    #    return redirect(url_for("login"))

    if not session.get("user"):
        session["flow"] = _build_auth_code_flow(scopes=app_config.SCOPE)
        return render_template('index.html', auth_url=session["flow"]["auth_uri"])
    else:
        return render_template('index.html', user=session["user"], company_profiles=company_profiles)

@app.route("/login")
def login():
    # Technically we could use empty list [] as scopes to do just sign in,
    # here we choose to also collect end user consent upfront
    session["flow"] = _build_auth_code_flow(scopes=app_config.SCOPE)
    return render_template("login.html", auth_url=session["flow"]["auth_uri"], version=msal.__version__)

@app.route(app_config.REDIRECT_PATH)  # Its absolute URL must match your app's redirect_uri set in AAD
def authorized():
    try:
        cache = _load_cache()
        result = _build_msal_app(cache=cache).acquire_token_by_auth_code_flow(
            session.get("flow", {}), request.args)
        if "error" in result:
            return render_template("auth_error.html", result=result)
        session["user"] = result.get("id_token_claims")
        _save_cache(cache)
    except ValueError:  # Usually caused by CSRF
        pass  # Simply ignore them
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()  # Wipe out user and its token cache from session
    return redirect(  # Also logout from your tenant's web session
        app_config.AUTHORITY + "/oauth2/v2.0/logout" +
        "?post_logout_redirect_uri=" + url_for("index", _external=True))

@app.route("/graphcall")
def graphcall():
    token = _get_token_from_cache(app_config.SCOPE)
    if not token:
        return redirect(url_for("login"))
    graph_data = requests.get(  # Use token to call downstream service
        app_config.ENDPOINT,
        headers={'Authorization': 'Bearer ' + token['access_token']},
        ).json()
    return render_template('graph.html', result=graph_data)


def _load_cache():
    cache = msal.SerializableTokenCache()
    if session.get("token_cache"):
        cache.deserialize(session["token_cache"])
    return cache

def _save_cache(cache):
    if cache.has_state_changed:
        session["token_cache"] = cache.serialize()

def _build_msal_app(cache=None, authority=None):
    return msal.ConfidentialClientApplication(
        app_config.CLIENT_ID, authority=authority or app_config.AUTHORITY,
        client_credential=app_config.CLIENT_SECRET, token_cache=cache)

def _build_auth_code_flow(authority=None, scopes=None):
    return _build_msal_app(authority=authority).initiate_auth_code_flow(
        scopes or [],
        redirect_uri=url_for("authorized", _external=True))

def _get_token_from_cache(scope=None):
    cache = _load_cache()  # This web app maintains one cache per session
    cca = _build_msal_app(cache=cache)
    accounts = cca.get_accounts()
    if accounts:  # So all account(s) belong to the current signed-in user
        result = cca.acquire_token_silent(scope, account=accounts[0])
        _save_cache(cache)
        return result

# Function to load company profiles from a JSON file
def load_company_profiles():
    try:
        with open('./database/company_profiles.json', 'r') as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

# Function to save company profiles to a JSON file
def save_company_profiles(profiles):
    with open('./database/company_profiles.json', 'w') as file:
        json.dump(profiles, file, indent=4)

# Load company profiles at the start
company_profiles = load_company_profiles()

@app.route("/company_profile", methods=['GET', 'POST'])
def company_profile():
    # Check if user is authenticated
    if not session.get("user"):
        return redirect(url_for("login"))
    if request.method == 'POST':
        # Process the form data
        company_name = request.form.get('company_name')
        account_number = request.form.get('account_number')
        # company_website = request.form.get('company_website')
        # business_phone = request.form.get('business_phone')
        # main_office_address = request.form.get('main_office_address')
        # address_line_1 = request.form.get('address_line_1')
        # address_line_2 = request.form.get('address_line_2')
        # city = request.form.get('city')
        # country = request.form.get('country')
        # working_hours = request.form.get('working_hours')
        # working_days = request.form.get('working_days')
        # work_arrangement = request.form.get('work_arrangement')

        # Add logic to save this data or process it as needed
        # Store profile data
        profile = {
            "id": len(company_profiles) + 1,
            "company_name": company_name,
            "account_number": account_number,
            # Add other fields...
        }
    
        company_profiles.append(profile)

        save_company_profiles(company_profiles)

        # Redirect to next page or acknowledge the submission
        return redirect(url_for('index'))  # Replace 'next_page' with your next route

    # Render the form page if method is GET
    return render_template('company_profile.html')


@app.route("/company_profile/<int:id>")
def view_company_profile(id):
    profile = next((p for p in company_profiles if p["id"] == id), None)
    if profile:
        return render_template("view_company_profile.html", profile=profile)
    else:
        return "Profile not found", 404

@app.route("/edit_company_profile/<int:id>", methods=["GET", "POST"])
def edit_company_profile(id):
    profile = next((p for p in company_profiles if p["id"] == id), None)
    if not profile:
        return "Profile not found", 404

    if request.method == "POST":
        # Process the form data and update the profile
        profile['company_name'] = request.form.get('company_name')
        profile['account_number'] = request.form.get('account_number')
        # Update other fields as necessary
        save_company_profiles(company_profiles)
        return redirect(url_for('view_company_profile', id=id))

    return render_template("edit_company_profile.html", profile=profile)

app.jinja_env.globals.update(_build_auth_code_flow=_build_auth_code_flow)  # Used in template

if __name__ == "__main__":
    app.run(debug=True)