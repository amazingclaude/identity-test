import uuid
import requests
from flask import Flask, render_template, session, request, redirect, url_for, has_request_context
from flask_session import Session  # https://pythonhosted.org/Flask-Session
import msal
import app_config
import json
import os
import openai
import copy
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()  # This loads the .env file at the project root

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
        # if session['last_update_time_before_editing'] does not exist, set it to empty dictionary
        if 'last_update_time_before_editing' not in session:
            session['last_update_time_before_editing'] ={}
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
#MY PROFILE
#*******************************
@app.route("/my_profile/view")
def my_profile():
    company_profile = load_company_profile()
    if 'standard_service' not in company_profile:
        company_profile['standard_service']=0
    if 'premium_service' not in company_profile:
        company_profile['premium_service']=0
    standard_service=company_profile['standard_service']
    premium_service=company_profile['premium_service']
    user=session["user"]
    return render_template("my_profile.html", user=user,standard_service=standard_service,premium_service=premium_service)


#*******************************
#COMPANY PROFILE
#*******************************
#Information on sub: https://learn.microsoft.com/en-us/azure/active-directory-b2c/tokens-overview
#It is the principal ID os the user, which is the unique identifier for the user account.

def get_company_file_path():
    if has_request_context() and 'user' in session:
        user_sub = session["user"].get("sub", "default")
    else:
        user_sub = "default"
    directory = os.path.join("./database", user_sub)
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
    if 'standard_service' not in company_profile:
        company_profile['standard_service']=0
        save_company_profile(company_profile)
    if 'premium_service' not in company_profile:
        company_profile['premium_service']=0
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
        user_sub = session["user"].get("sub", "default")
    else:
        user_sub = "default"

    directory = os.path.join("./database", user_sub)
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


def update_profile_from_form(profile, form_data):
    profile_updated = False  # Flag to track changes

    # Update profile fields
    for field in ['job_title', 'report_to', 'have_reports', 'vacancy_number', 
                  'job_reponsibilities', 'ideal_candidate', 'other_info', 
                  'full_or_parttime', 'job_type', 'fixed_term_reason', 
                  'pay_contractor', 'salary_range_min', 'salary_range_max', 
                  'working_hours', 'working_days', 'work_arrangement', 
                  'job_location', 'visa_sponsor', 'additional_note']:
        if field not in profile or profile.get(field) != form_data.get(field):
            profile[field] = form_data.get(field)
            profile_updated = True

    if profile_updated:
        profile['profile_updated_at'] = datetime.utcnow().isoformat()

    return profile


@app.route("/job_profile", methods=['GET', 'POST'])
def create_job_profile():
    job_profiles = load_job_profiles()
    existing_ids = set(p["job_id"] for p in job_profiles)

    # Creating a new profile
    new_job_id = 1
    while new_job_id in existing_ids:
        new_job_id += 1

    profile = {"job_id": new_job_id, 
               'profile_updated_at': 0, 
               'generated_ad': '', 
               'fixed_term_reason': 'Not Available', 
               'pay_contractor': 'Not Available', 
               'job_status': 'Draft'}

    if new_job_id not in existing_ids:
        job_profiles.append(profile)

    # Store the last update time before editing
    session['last_update_time_before_editing'][new_job_id] =profile['profile_updated_at']


    if request.method == 'POST':
        profile = update_profile_from_form(profile, request.form)
        save_job_profiles(job_profiles)
        return redirect(url_for('view_job_profile', job_id=new_job_id, job_status=profile['job_status']))

    return render_template("job_profile.html", profile=profile, user=session["user"], new_create_job_indicator=1)



@app.route("/job_profile/edit/<int:job_id>", methods=['GET', 'POST'])
def edit_job_profile(job_id):
    job_profiles = load_job_profiles()
    existing_ids = set(p["job_id"] for p in job_profiles)

    if job_id not in existing_ids:
        return "Profile not found", 404

    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)

    # Store the last update time before editing
    session['last_update_time_before_editing'][job_id]=profile['profile_updated_at']

    if request.method == 'POST':
        profile = update_profile_from_form(profile, request.form)
        save_job_profiles(job_profiles)
        return redirect(url_for('view_job_profile', job_id=job_id, job_status=profile['job_status']))

    return render_template("job_profile.html", profile=profile, user=session["user"], new_create_job_indicator=0)



@app.route("/job_profile/view/<int:job_id>")
def view_job_profile(job_id): 
    job_profiles = load_job_profiles() 

    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)

    # if session['last_update_time_before_editing'] does not exist, set it to profile['profile_updated_at']
    # Because for user who signed out and signed back in, session is cleared, hence session['last_update_time_before_editing'] will not exist
    if 'last_update_time_before_editing' not in session:
        session['last_update_time_before_editing'] = {}
        session['last_update_time_before_editing'][job_id] = profile['profile_updated_at']
    

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

#Clone job profile
@app.route("/clone_job_profile/<int:job_id>", methods=["POST"])
def clone_job_profile(job_id):
    job_profiles = load_job_profiles()
    existing_ids = set(p["job_id"] for p in job_profiles)

    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)
    new_profile=copy.deepcopy(profile)

    # Creating a new profile
    new_job_id = 1
    while new_job_id in existing_ids:
        new_job_id += 1

    #Assign new job_id to the newly created profile
    new_profile["job_id"] = new_job_id

    if not new_job_id in existing_ids:
        job_profiles.append(new_profile)

    # add the profile update time for the new job_id to session 
    session['last_update_time_before_editing'][new_job_id] = profile['profile_updated_at']  

    save_job_profiles(job_profiles)
    return redirect(url_for('index'))


#*******************************
#JOB AD
#*******************************

def call_azure_open_ai(job_profile_description):
    openai.api_key = os.getenv("AZURE_OPENAI_KEY")
    openai.api_base = os.getenv("AZURE_OPENAI_ENDPOINT") # your endpoint should look like the following https://YOUR_RESOURCE_NAME.openai.azure.com/
    openai.api_type = 'azure'
    openai.api_version = '2023-07-01-preview' # this might change in the future

    deployment_name='zispire_openai' #This will correspond to the custom name you chose for your deployment when you deployed a model. 
    message_text = [{"role":"system",
                     "content":"You are a Job Recruiter Assistant that helps HR to generate job advertisement."},
                    {"role":"user",
                    "content":job_profile_description}
                     ]

    # Make a POST request to Azure OpenAI's GPT model with the job profile description
    response = openai.ChatCompletion.create(
        engine=deployment_name,
        messages=message_text,
        temperature=0.7,
        max_tokens=200,
        top_p=0.95,
        frequency_penalty=0,
        presence_penalty=0,
        stop=None
        # stop=["\n", "Human:", "AI:"]
    )

    generated_ad = response.get("choices")[0]['message']['content']
    return generated_ad

def generate_job_ad(profile):
    job_profile_description = f"""
    Based on the job profile provided, generate job advertisement in plain text. Only show the generated job advertisement in your answer.
    Job Title: {profile.get('job_title', '')}
    Responsibilities: {profile.get('job_reponsibilities', '')}
    Ideal Candidate: {profile.get('ideal_candidate', '')}
    Salary Range: {profile.get('salary_range_min', '')} - {profile.get('salary_range_max', '')}
    Working Hours: {profile.get('working_hours', '')}
    Location: {profile.get('job_location', '')}
    Additional Notes: {profile.get('additional_notes', '')}
    """
    return call_azure_open_ai(job_profile_description)


@app.route("/create_job_ad/regenerate/<int:job_id>")
def regenerate_job_ad(job_id):
    job_profiles = load_job_profiles()
    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)

    if not profile:
        return "Job profile not found", 404

    generated_ad = generate_job_ad(profile)
    profile['generated_ad'] = generated_ad
    save_job_profiles(job_profiles)
    session['last_update_time_before_editing'][job_id]= profile['profile_updated_at']
    return render_template("job_ad.html", job_ad=html_content, job_id=job_id, user=session["user"])


@app.route("/create_job_ad/<int:job_id>")
def create_job_ad(job_id):
    
    job_profiles = load_job_profiles()
    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)



    if not profile:
        return "Job profile not found", 404
    
    # Check if the profile has been updated since the last time the job ad was generated
    # If yes, set the profile_updated_indicator to 1, otherwise 0
    # This is to prevent the job ad from being regenerated when the user clicks on the 'Create Job Ad' button
    # The job ad will only be regenerated when the user clicks on the 'Regenerate Job Ad' button
    
    if profile['profile_updated_at'] != session['last_update_time_before_editing'][job_id] and session['last_update_time_before_editing'][job_id] != 0 :
        profile_updated_indicator = 1
    else:    
        profile_updated_indicator = 0

    # Check if 'generated_ad' is empty, if yes, generate the job ad
    if profile['generated_ad'] == '':
        generated_ad = generate_job_ad(profile)
        profile['generated_ad'] = generated_ad
        save_job_profiles(job_profiles)
        html_content = generated_ad.replace("\n", "<br>")
    
    else:
        html_content = profile['generated_ad'].replace("\n", "<br>")
    
    return render_template("job_ad.html", job_ad=html_content, job_id=job_id,profile_updated_indicator=profile_updated_indicator, user=session["user"])

@app.route("/edit_job_ad/<int:job_id>", methods=["GET", "POST"])
def edit_job_ad(job_id):
    user=session["user"]
    job_profiles = load_job_profiles()  # Load your job profiles

    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)

    if profile['profile_updated_at'] != session['last_update_time_before_editing'][job_id] and session['last_update_time_before_editing'][job_id] != 0 :
        profile_updated_indicator = 1
    else:    
        profile_updated_indicator = 0

    if not profile:
        return "Job profile not found", 404

    if request.method == "POST":
        # Update the 'generated_ad' in the profile with the new content from the form
        profile['generated_ad'] = request.form['generated_ad_content']

        # Save the updated profiles back to your storage
        save_job_profiles(job_profiles)

        edited_ad= copy.deepcopy(profile['generated_ad'])
        html_content = edited_ad.replace("\n", "<br>")
        # Redirect to the view page or somewhere else after saving
        return render_template("job_ad.html", job_ad=html_content, job_id=job_id, profile_updated_indicator=profile_updated_indicator, user=user)

    return render_template("edit_job_ad.html", profile=profile, user=user)

@app.route("/payment" , methods=["GET", "POST"])
def payment():
    company_profile = load_company_profile()
    user=session["user"]
    if 'standard_service' not in company_profile:
        company_profile['standard_service']=0
    if 'premium_service' not in company_profile:
        company_profile['premium_service']=0
        

    if request.method == "POST":
         # Retrieve data from the form
        selected_service = request.form.get('selectedService')
        selected_amount = request.form.get('numberOfReqs')

        if selected_service=='standardService':
            company_profile['standard_service']=company_profile['standard_service']+int(selected_amount)
        elif selected_service=='premiumService':
            company_profile['premium_service']=company_profile['premium_service']+int(selected_amount)
        save_company_profile(company_profile)
        return render_template("my_profile.html", user=user,standard_service=company_profile['standard_service'],premium_service=company_profile['premium_service'])
    return render_template("payment.html", user=user)

@app.route("/checkout/<int:job_id>", methods=["GET", "POST"])
def checkout(job_id):
    user=session["user"]
    company_profile = load_company_profile()
    if 'standard_service' not in company_profile:
        company_profile['standard_service']=0
    if 'premium_service' not in company_profile:
        company_profile['premium_service']=0
    standard_service=company_profile['standard_service']
    premium_service=company_profile['premium_service']

    if request.method == "POST":
        selected_service = request.form.get('serviceType')

        if selected_service=='standardService':
            company_profile['standard_service']=standard_service-1
        elif selected_service=='premiumService':
            company_profile['premium_service']=premium_service-1
        save_company_profile(company_profile)

        job_profiles = load_job_profiles()
        profile = next((p for p in job_profiles if p["job_id"] == job_id), None)
        profile['job_status']='Submitted'
        save_job_profiles(job_profiles)
        
        return render_template("checkout_success.html", user=user,job_id=job_id)
    
    return render_template("checkout.html", user=user,standard_service=standard_service,premium_service=premium_service, job_id=job_id)


app.jinja_env.globals.update(_build_auth_code_flow=_build_auth_code_flow)  # Used in template

if __name__ == "__main__":
    app.run(debug=True)