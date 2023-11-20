import uuid
import requests
from flask import Flask, render_template, session, request, redirect, url_for, has_request_context
from flask_session import Session  # https://pythonhosted.org/Flask-Session
import msal
import app_config
import json
import os

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
        # Load job profiles at the start
        job_profiles = load_job_profiles() 
        sort_order = request.args.get('sort', 'asc')
        if sort_order == 'desc':
            job_profiles.sort(key=lambda x: x['job_id'], reverse=True)
        else:
            job_profiles.sort(key=lambda x: x['job_id'])   
        return render_template('index.html', user=session["user"], job_profiles=job_profiles)

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
    
#*******************************
#COMPANY PROFILE
#*******************************


def get_company_file_path():
    if has_request_context() and 'user' in session:
        user_aud = session["user"].get("aud", "default")
    else:
        user_aud = "default"
    directory = os.path.join("./database", user_aud)
    if not os.path.exists(directory):
        os.makedirs(directory)
    return os.path.join(directory, 'company_profile.json')

def load_company_profile():
    try:
        with open(get_company_file_path(), 'r') as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_company_profile(profile):
    with open(get_company_file_path(), 'w') as file:
        json.dump(profile, file, indent=4)


@app.route("/company_profile/view")
def view_company_profile():
    company_profile = load_company_profile()
    user=session["user"]
    # Check if 'company_name' is not in the dictionary, if not, it means it is first time registration, hence we auto fill the info from Azure B2C
    if 'company_name' not in company_profile:
        company_profile['company_name'] = user.get('extension_CompanyName', 'unknown')
        save_company_profile(company_profile)
    return render_template("view_company_profile.html", profile=company_profile, user=user )


@app.route("/company_profile/edit", methods=["GET", "POST"])
def edit_company_profile():
    company_profile = load_company_profile()
    user=session["user"]

    if request.method == "POST":
        # Process form data and update company_profile dictionary
        company_profile['company_name'] = request.form.get('company_name')
        company_profile['company_website'] = request.form.get('company_website')
        company_profile['business_phone'] = request.form.get('business_phone')
        company_profile['main_office_address'] = request.form.get('main_office_address')
        company_profile['address_line_1'] = request.form.get('address_line_1')
        company_profile['address_line_2'] = request.form.get('address_line_2')
        company_profile['city'] = request.form.get('city')
        company_profile['country'] = request.form.get('country')
        company_profile['working_hours'] = request.form.get('working_hours')
        company_profile['working_days'] = request.form.get('working_days')
        company_profile['work_arrangement'] = request.form.get('work_arrangement')
        # Update other fields as necessary
        


        save_company_profile(company_profile)
        return redirect(url_for('view_company_profile'))

    return render_template("edit_company_profile.html", profile=company_profile, user=user)


#*******************************
#JOB PROFILE
#*******************************

def get_profile_file_path():
    if has_request_context() and 'user' in session:
        user_aud = session["user"].get("aud", "default")
    else:
        user_aud = "default"

    directory = os.path.join("./database", user_aud)
    if not os.path.exists(directory):
        os.makedirs(directory)
    return os.path.join(directory, 'job_profiles.json')


def load_job_profiles():
    try:
        with open(get_profile_file_path(), 'r') as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_job_profiles(profiles):
    with open(get_profile_file_path(), 'w') as file:
        json.dump(profiles, file, indent=4)


@app.route("/job_profile", defaults={'job_id': None}, methods=['GET', 'POST'])
@app.route("/job_profile/edit/<int:job_id>", methods=['GET', 'POST'])
def job_profile(job_id):
    job_profiles = load_job_profiles()
    existing_ids = set(p["job_id"] for p in job_profiles)

    #Check if it is new job creation, the logic will make job_id+1 if job_id!=None, hence a supplement condition added (job_id in existing_ids): 
    if job_id is not None and job_id in existing_ids:
        # Editing an existing profile
        profile = next((p for p in job_profiles if p["job_id"] == job_id), None)
        #Create a new_create_session flag to deliver to job_profile.html, to give Cancel button two choices, either back to view page or back to index page.
        new_create_session=0
        if not profile:
            return "Profile not found", 404
    else:
        # Creating a new profile
        new_job_id = 1
        while new_job_id in existing_ids:
            new_job_id += 1
        profile = {"job_id": new_job_id, "job_title": "", "report_to": "", "have_reports": "No"}
        new_create_session=1
        if not new_job_id in existing_ids:
            job_profiles.append(profile)


    if request.method == 'POST':
        profile['job_title'] = request.form.get('job_title')
        profile['report_to'] = request.form.get('report_to')
        profile['have_reports'] = request.form.get('have_reports')

        save_job_profiles(job_profiles)

        return redirect(url_for('view_job_profile', job_id=job_id))
    return render_template("job_profile.html", profile=profile, user=session["user"],new_create_session=new_create_session)



@app.route("/job_profile/view/<int:job_id>")
def view_job_profile(job_id): 
    job_profiles = load_job_profiles() 

    print("Loaded profiles:", job_profiles)

    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)

    print("Found profile:", profile)

    if profile:
        return render_template("view_job_profile.html", profile=profile, user=session["user"])
    else:
        return "Profile not found", 404

@app.route("/delete_job_profile/<int:job_id>", methods=["POST"])
def delete_job_profile(job_id):
    job_profiles = load_job_profiles()
    job_profiles = [profile for profile in job_profiles if profile["job_id"] != job_id]
    save_job_profiles(job_profiles)
    return redirect(url_for('index'))

app.jinja_env.globals.update(_build_auth_code_flow=_build_auth_code_flow)  # Used in template

if __name__ == "__main__":
    app.run(debug=True)