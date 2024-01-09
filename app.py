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

from azure.cosmos import CosmosClient, exceptions

import stripe

from dotenv import load_dotenv
load_dotenv()  # This loads the .env file at the project root



app = Flask(__name__)
app.config.from_object(app_config)
Session(app)

# Initialize the Cosmos DB client
client = CosmosClient(app_config.ACCOUNT_HOST, credential=app_config.ACCOUNT_KEY)
database = client.get_database_client(app_config.COSMOS_DATABASE)
container = database.get_container_client(app_config.COSMOS_CONTAINER)

stripe.api_key = app_config.STRIPE_KEY

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
    if not session.get("user"):
        session["flow"] = _build_auth_code_flow(scopes=app_config.SCOPE)
        return render_template('index.html', auth_url=session["flow"]["auth_uri"])
    else:
        job_profiles_doc = load_job_profiles() 
        job_profiles = job_profiles_doc['job_profiles']

        # Get filter and sort parameters from the request
        show_deleted = request.args.get('show_deleted', 'no')
        job_status = request.args.get('job_status', 'all')
        sort_order = request.args.get('sort', 'asc')

        # Apply filters
        if show_deleted == 'no':
            job_profiles = [profile for profile in job_profiles if not profile.get('deleted', False)]
        if job_status != 'all':
            job_profiles = [profile for profile in job_profiles if profile.get('job_status') == job_status]

        # Apply sorting
        job_profiles.sort(key=lambda x: x['job_id'], reverse=(sort_order == 'desc'))

        return render_template('index.html', user=session["user"], job_profiles=job_profiles, 
                               show_deleted=show_deleted, job_status=job_status, sort_order=sort_order)


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
#Information on sub (subject): https://learn.microsoft.com/en-us/azure/active-directory-b2c/tokens-overview
#It is the principal ID os the user, which is the unique identifier for the user account.



def get_user_sub():
    '''Returns the user's sub (subject) ID, which is the unique identifier for the user account.'''
    if has_request_context() and 'user' in session:
        return session["user"].get("sub", "default")
    else:
        return "default"

def query_container(query, parameters):
    try:
        return list(container.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=True
        ))
    except exceptions.CosmosHttpResponseError:
        return {}

def load_company_profile():
    user_id = get_user_sub()
    doc_id=user_id
    items = query_container("SELECT * FROM c WHERE c.id = @id", [{"name": "@id", "value": doc_id}])  
    return items[0] if items else {}

def save_document(document):
    try:
        container.upsert_item(document)
    except exceptions.CosmosHttpResponseError as e:
        print(f'An error occurred: {e}')

@app.route("/company_profile/view")
def view_company_profile():
    company_profile = load_company_profile()
    user = session["user"]
    user_id=user.get("sub", "default")
    # Initialize missing fields with default values if not present
    fields_to_initialize = {
        'id': user_id,
        'user_id':user_id,
        'company_name': user.get('extension_CompanyName', 'unknown'),
        'standard_service': 0,
        'premium_service': 0
    }

    # Check and initialize missing fields
    update_required = False
    for field, default_value in fields_to_initialize.items():
        if field not in company_profile:
            company_profile[field] = default_value
            update_required = True

    # Save updates if any field was initialized
    if update_required:
        save_document(company_profile)

    return render_template("view_company_profile.html", profile=company_profile, user=user)


@app.route("/company_profile/edit", methods=["GET", "POST"])
def edit_company_profile():
    company_profile = load_company_profile()
    user = session["user"]

    if request.method == "POST":
        # Process form data and update company_profile dictionary
        fields_to_update = [
            'company_name', 'company_website', 'business_phone', 
            'main_office_address', 'address_line_1', 'address_line_2',
            'city', 'country', 'working_hours', 'working_days', 
            'work_arrangement'
            # Add other fields as necessary
        ]

        for field in fields_to_update:
            company_profile[field] = request.form.get(field, '')

        save_document(company_profile)
        return redirect(url_for('view_company_profile'))

    return render_template("edit_company_profile.html", profile=company_profile, user=user)

#*******************************
#JOB PROFILE
#*******************************

def load_job_profiles():
    user_id=get_user_sub()
    doc_id = user_id + '_job'
    items = query_container("SELECT * FROM c WHERE c.id = @id", [{"name": "@id", "value": doc_id}])
    #if item is empty, initialize the job profile
    if not items:
        job_profiles_doc_initialize = {
            'id': doc_id,
            'user_id':user_id,
            'job_profiles': []
        }
        save_document(job_profiles_doc_initialize)
        return job_profiles_doc_initialize
    return items[0]

def update_profile_from_form(profile, form_data):
    profile_updated = False  # Flag to track changes

    # Update profile fields
    for field in ['job_title', 'report_to', 'have_reports', 
                  'job_reponsibilities', 'ideal_candidate', 'other_info', 
                  'full_or_parttime', 'job_type', 'fixed_term_reason', 
                  'pay_contractor', 'salary_type','salary_range_min', 'salary_range_max', 
                  'working_hours', 'working_days', 'work_arrangement', 
                  'job_location', 'visa_sponsor', 'additional_note']:
        if field not in profile or profile.get(field) != form_data.get(field):
            profile[field] = form_data.get(field)
            profile_updated = True

    if profile_updated:
        profile['profile_updated_at'] = datetime.utcnow().isoformat()
        profile['alow_ad_generation'] = True
        
    return profile


@app.route("/job_profile", methods=['GET', 'POST'])
def create_job_profile():
    job_profiles_doc = load_job_profiles()   
    job_profiles = job_profiles_doc['job_profiles']

    # Find the maximum existing job_id and add 1 to it to get the new_job_id
    # If no profiles exist, start with 1
    new_job_id = max(p["job_id"] for p in job_profiles) + 1 if job_profiles else 1

    # Define the new profile
    profile = {
        "job_id": new_job_id, 
        'profile_updated_at': 0, 
        'allow_ad_generation': True,
        'generated_ad': '', 
        'fixed_term_reason': 'Not Available', 
        'pay_contractor': 'Not Available', 
        'job_status': 'Draft',
        'job_deleted': False  # New field to indicate deletion status
    }

    # Append the new profile to job_profiles
    job_profiles.append(profile)

    if request.method == 'POST':
        profile = update_profile_from_form(profile, request.form)
        save_document(job_profiles_doc)
        return redirect(url_for('view_job_profile', job_id=new_job_id, job_status=profile['job_status']))

    return render_template("job_profile.html", profile=profile, user=session["user"], new_create_job_indicator=1)



@app.route("/job_profile/edit/<int:job_id>", methods=['GET', 'POST'])
def edit_job_profile(job_id):
    job_profiles_doc = load_job_profiles()
    job_profiles = job_profiles_doc['job_profiles']

    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)


    if request.method == 'POST':
        profile = update_profile_from_form(profile, request.form)
        save_document(job_profiles_doc)
        return redirect(url_for('view_job_profile', job_id=job_id, job_status=profile['job_status']))

    return render_template("job_profile.html", profile=profile, user=session["user"], new_create_job_indicator=0)



@app.route("/job_profile/view/<int:job_id>")
def view_job_profile(job_id): 
    job_profiles_doc = load_job_profiles() 
    job_profiles = job_profiles_doc['job_profiles']
    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)

    if profile:
        return render_template("view_job_profile.html", profile=profile, user=session["user"])
    else:
        return "Profile not found", 404

@app.route("/delete_job_profile/<int:job_id>", methods=["POST"])
def delete_job_profile(job_id):
    job_profiles_doc = load_job_profiles()
    job_profiles = job_profiles_doc['job_profiles']
    for profile in job_profiles:
        if profile["job_id"] == job_id:
            profile['job_deleted'] = True
            break
    save_document(job_profiles_doc)
    return redirect(url_for('index'))

# Filter out deleted profiles in your view
def get_active_profiles(job_profiles):
    return [profile for profile in job_profiles if not profile['job_deleted']]

@app.route("/recover_job_profile/<int:job_id>", methods=["POST"])
def recover_job_profile(job_id):
    job_profiles_doc = load_job_profiles()
    job_profiles = job_profiles_doc['job_profiles']
    for profile in job_profiles:
        if profile["job_id"] == job_id:
            profile['job_deleted'] = False  # Set the deleted flag back to False
            break
    save_document(job_profiles_doc)
    return redirect(url_for('index'))

#Clone job profile
@app.route("/clone_job_profile/<int:job_id>", methods=["POST"])
def clone_job_profile(job_id):
    job_profiles_doc = load_job_profiles()
    job_profiles = job_profiles_doc['job_profiles']
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


    save_document(job_profiles_doc)
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

def generate_job_ad(profile,company_profile):
    job_profile_description = f"""
    Based on the job profile and company profile provided after ===, generate job advertisement in plain text. Only show the generated job advertisement in your answer.
    Part 1, Top Selling Points. Top 3 selling point or benefits of the company (if remote or hybrid is mentioned, display it as a selling point)
    Part 2, About the company. Do not show the company name. 
    Part 3, About the role. Describle what the role does or what the purpose of the role is. Descript the key responsibilities as bulltin points, up to 10. 
    Part 4, Our Ideal Candidates. Describle the ideal candidate including the experience, skills, qualifications, and other requirements supplied
    All information generated should contain minimum amendment to the provided job profile. Include a closure phrase to encourage candidates to apply now.  
    ===
    Job Title: {profile.get('job_title', '')}
    Responsibilities: {profile.get('job_reponsibilities', '')}
    Ideal Candidate: {profile.get('ideal_candidate', '')}
    Other Information: {profile.get('other_info', '')}
    Salary Range: {profile.get('salary_range_min', '')} - {profile.get('salary_range_max', '')}
    Working Hours: {profile.get('working_hours', '')}
    Location: {profile.get('job_location', '')}
    Additional Notes: {profile.get('additional_notes', '')}
    Company's business: { company_profile.get('CompanyQ1', '') }
    Company's customers: { company_profile.get('CompanyQ2', '') }
    Employee benefits to offer: { company_profile.get('CompanyQ3', '') }
    Top 3 reasons people should work for the company? { company_profile.get('CompanyQ4', '') }
    """
    return call_azure_open_ai(job_profile_description)


@app.route("/create_job_ad/regenerate/<int:job_id>")
def regenerate_job_ad(job_id):
    company_profile=load_company_profile()
    job_profiles_doc = load_job_profiles()
    job_profiles = job_profiles_doc['job_profiles']
    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)

    if not profile:
        return "Job profile not found", 404
    if profile['alow_ad_generation'] == False:
        html_content = profile['generated_ad'].replace("\n", "<br>")
        return render_template("job_ad.html", job_ad=html_content, job_id=job_id, user=session["user"])
    else:
        generated_ad = generate_job_ad(profile,company_profile)
        profile['generated_ad'] = generated_ad
      
        profile['alow_ad_generation'] = False
        save_document(job_profiles_doc)
        html_content = generated_ad.replace("\n", "<br>")
        return render_template("job_ad.html", job_ad=html_content, job_id=job_id, user=session["user"])


@app.route("/create_job_ad/<int:job_id>")
def create_job_ad(job_id):
    company_profile=load_company_profile()
    job_profiles_doc = load_job_profiles()
    job_profiles = job_profiles_doc['job_profiles']
    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)

    if not profile:
        return "Job profile not found", 404
    

    # Check if the profile has been updated since the last time the job ad was generated
    # If yes, set the profile_updated_indicator to 1, otherwise 0
    # This is to prevent the job ad from being regenerated when the user clicks on the 'Create Job Ad' button
    # The job ad will only be regenerated when the user clicks on the 'Regenerate Job Ad' button
    
    if profile['alow_ad_generation'] == True:
        profile_updated_indicator = 1
    else:    
        profile_updated_indicator = 0

    # Check if 'generated_ad' is empty, if yes, generate the job ad
    if profile['generated_ad'] == '':
        generated_ad = generate_job_ad(profile,company_profile)
        profile['generated_ad'] = generated_ad
        profile['alow_ad_generation'] = False
        save_document(job_profiles_doc)
        html_content = generated_ad.replace("\n", "<br>")
    
    else:
        html_content = profile['generated_ad'].replace("\n", "<br>")
    
    return render_template("job_ad.html", job_ad=html_content, job_id=job_id,profile_updated_indicator=profile_updated_indicator, user=session["user"])

@app.route("/edit_job_ad/<int:job_id>", methods=["GET", "POST"])
def edit_job_ad(job_id):
    user=session["user"]
    job_profiles_doc = load_job_profiles()  # Load your job profiles
    job_profiles = job_profiles_doc['job_profiles']
    profile = next((p for p in job_profiles if p["job_id"] == job_id), None)

    if profile['alow_ad_generation'] == True:
        profile_updated_indicator = 1
    else:    
        profile_updated_indicator = 0

    if not profile:
        return "Job profile not found", 404

    if request.method == "POST":
        # Update the 'generated_ad' in the profile with the new content from the form
        profile['generated_ad'] = request.form['generated_ad_content']

        # Save the updated profiles back to your storage
        save_document(job_profiles_doc)

        edited_ad= copy.deepcopy(profile['generated_ad'])
        html_content = edited_ad.replace("\n", "<br>")
        # Redirect to the view page or somewhere else after saving
        return render_template("job_ad.html", job_ad=html_content, job_id=job_id, profile_updated_indicator=profile_updated_indicator, user=user)

    return render_template("edit_job_ad.html", profile=profile, user=user)


@app.route("/checkout/<int:job_id>", methods=["GET", "POST"])
def checkout(job_id):
    '''
    This function is used to consume the purchased quote.
    '''
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
        save_document(company_profile)

        job_profiles_doc = load_job_profiles()
        job_profiles = job_profiles_doc['job_profiles']
        profile = next((p for p in job_profiles if p["job_id"] == job_id), None)
        profile['job_status']='Submitted'
        save_document(job_profiles_doc)
        
        return render_template("checkout_success.html", user=user,job_id=job_id)
    
    return render_template("checkout.html", user=user,standard_service=standard_service,premium_service=premium_service, job_id=job_id)

price_dict = {
        'premiumService': {'1': 'price_1OW1rkA8ljhYPX0FsffKjjX1', '2': 'price_1OW1uHA8ljhYPX0FQZQswxSv', '3': 'price_1OW1wWA8ljhYPX0FnzbzfYB3'},
        'standardService': {'1': 'price_1OW105A8ljhYPX0F0CSmoO4M', '2': 'price_1OW1oIA8ljhYPX0FSJrK6kZy', '3': 'price_1OW1pOA8ljhYPX0FXqA8FaX8'}
    }


MY_DOMAIN = app_config.MY_DOMAIN  

@app.route("/payment", methods=["GET", "POST"])
def payment():
    user = session["user"]

    if request.method == "POST":
        selected_service = request.form.get('selectedService')
        selected_amount = request.form.get('numberOfReqs')

        # Retrieve the price ID from price_dict based on the selected service and amount
        price_id = price_dict[selected_service][selected_amount]

        try:
            checkout_session = stripe.checkout.Session.create(
                line_items=[
                    {
                        'price': price_id,
                        'quantity': int(selected_amount),
                    },
                ],
                mode='payment',
                success_url=MY_DOMAIN+'/stripe_success.html',
                cancel_url=MY_DOMAIN+'/stripe_cancel.html',
                automatic_tax={'enabled': True},
                metadata={
                    'selected_service': selected_service,
                    'selected_amount': selected_amount
                }
            )
            return redirect(checkout_session.url, code=303)
        except Exception as e:
            # Handle exceptions by returning an error message or redirecting to an error page
            return str(e)

    # If it's a GET request or any other method, render the payment page
    return render_template("payment.html", user=user)


@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.data
    event = None

    try:
        event = json.loads(payload)
    except ValueError as e:
        return '⚠️ Webhook error while parsing basic request.', 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']

        # Retrieve the selected service and amount from the session
        selected_service = session.get('metadata').get('selected_service')
        selected_amount = session.get('metadata').get('selected_amount')

        # Load the company profile and update it
        company_profile = load_company_profile()
        if 'standard_service' not in company_profile:
            company_profile['standard_service'] = 0
        if 'premium_service' not in company_profile:
            company_profile['premium_service'] = 0

        # Update the company profile based on the completed checkout session
        if selected_service == 'standardService':
            company_profile['standard_service'] += int(selected_amount)
        elif selected_service == 'premiumService':
            company_profile['premium_service'] += int(selected_amount)

        # Save the updated company profile
        save_document(company_profile)

        print(f"Payment for {session['amount_total']} succeeded")
        # Define and call a method to handle the successful payment intent if needed

    # ... [handle other event types]

    return jsonify(success=True)

@app.route('/success', methods=['GET'])
def order_success():
    try:
        session = stripe.checkout.Session.retrieve(request.args.get('session_id'))
        # TODO: Add customer info
        # customer = stripe.Customer.retrieve(session.customer)

        return render_template('stripe_sucess.html')
    except Exception as e:
        # Handle exceptions and possibly return an error message or page
        print(f"Error retrieving order details: {e}")
        return "An error occurred", 500
    
@app.route('/cancel', methods=['GET'])
def cancel_order():
    # Here you can add any logic you might need to handle a canceled order
    return render_template('stripe_cancel.html')  # Render a cancel page or message

app.jinja_env.globals.update(_build_auth_code_flow=_build_auth_code_flow)  # Used in template

if __name__ == "__main__":
    app.run(debug=True)