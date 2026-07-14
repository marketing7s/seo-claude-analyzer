import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
# Hardcoded default secret key - no env var needed
# You can still override with SECRET_KEY env var if you want extra security
app.secret_key = os.environ.get('SECRET_KEY', 'seo-analyzer-default-secret-key-2026')

# Service account JSON key (contents of the downloaded .json file)
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')

# OAuth scopes needed
SCOPES = [
    'https://www.googleapis.com/auth/webmasters.readonly',
    'https://www.googleapis.com/auth/analytics.readonly'
]

def get_credentials():
    """Build credentials from service account JSON stored in environment"""
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    
    try:
        service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=SCOPES
        )
        return creds
    except Exception as e:
        print(f"Error building credentials: {e}")
        return None

def fetch_gsc_data(creds, site_url, days=30):
    try:
        service = build('webmasters', 'v3', credentials=creds)
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        request_body = {
            'startDate': start_date,
            'endDate': end_date,
            'dimensions': ['query'],
            'rowLimit': 50,
            'startRow': 0
        }
        
        response = service.searchanalytics().query(siteUrl=site_url, body=request_body).execute()
        rows = response.get('rows', [])
        
        summary_request = {
            'startDate': start_date,
            'endDate': end_date,
            'dimensions': [],
            'rowLimit': 1
        }
        summary_response = service.searchanalytics().query(siteUrl=site_url, body=summary_request).execute()
        summary_rows = summary_response.get('rows', [])
        
        return {
            'rows': rows,
            'summary': summary_rows[0] if summary_rows else {},
            'date_range': {'start': start_date, 'end': end_date}
        }
    except Exception as e:
        return {'error': str(e), 'rows': [], 'summary': {}}

def fetch_gsc_pages(creds, site_url, days=30):
    try:
        service = build('webmasters', 'v3', credentials=creds)
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        request_body = {
            'startDate': start_date,
            'endDate': end_date,
            'dimensions': ['page'],
            'rowLimit': 25,
            'startRow': 0
        }
        
        response = service.searchanalytics().query(siteUrl=site_url, body=request_body).execute()
        return response.get('rows', [])
    except Exception as e:
        return [{'error': str(e)}]

def fetch_ga4_data(creds, property_id, days=30):
    try:
        service = build('analyticsdata', 'v1beta', credentials=creds)
        property_name = f'properties/{property_id}'
        
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        request_body = {
            'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
            'dimensions': [
                {'name': 'pageTitle'},
                {'name': 'pagePath'}
            ],
            'metrics': [
                {'name': 'sessions'},
                {'name': 'totalUsers'},
                {'name': 'screenPageViews'},
                {'name': 'bounceRate'},
                {'name': 'averageSessionDuration'}
            ],
            'limit': 25
        }
        
        pages_response = service.properties().runReport(property=property_name, body=request_body).execute()
        
        sources_request = {
            'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
            'dimensions': [{'name': 'sessionDefaultChannelGroup'}],
            'metrics': [
                {'name': 'sessions'},
                {'name': 'totalUsers'}
            ],
            'limit': 10
        }
        
        sources_response = service.properties().runReport(property=property_name, body=sources_request).execute()
        
        overall_request = {
            'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
            'metrics': [
                {'name': 'sessions'},
                {'name': 'totalUsers'},
                {'name': 'screenPageViews'},
                {'name': 'bounceRate'},
                {'name': 'averageSessionDuration'},
                {'name': 'newUsers'}
            ]
        }
        
        overall_response = service.properties().runReport(property=property_name, body=overall_request).execute()
        
        return {
            'pages': pages_response.get('rows', []),
            'sources': sources_response.get('rows', []),
            'overall': overall_response.get('rows', []),
            'date_range': {'start': start_date, 'end': end_date}
        }
    except Exception as e:
        return {'error': str(e), 'pages': [], 'sources': [], 'overall': []}

def generate_claude_prompt(project_name, gsc_url, ga4_property_id, question, gsc_data, gsc_pages, ga4_data, days):
    prompt = f"""I need you to analyze the following SEO and Analytics data and answer my question.

PROJECT: {project_name}
GSC Site: {gsc_url}
GA4 Property: {ga4_property_id}

MY QUESTION: {question}

DATE RANGE: Last {days} days ({gsc_data.get('date_range', {}).get('start', 'N/A')} to {gsc_data.get('date_range', {}).get('end', 'N/A')})

================================================================================
GOOGLE SEARCH CONSOLE - OVERALL PERFORMANCE
================================================================================
"""
    
    summary = gsc_data.get('summary', {})
    if summary:
        clicks = summary.get('clicks', 0)
        impressions = summary.get('impressions', 0)
        ctr = summary.get('ctr', 0)
        position = summary.get('position', 0)
        prompt += f"""Total Clicks: {clicks:,.0f}
Total Impressions: {impressions:,.0f}
Average CTR: {ctr:.2%}
Average Position: {position:.1f}

"""
    else:
        prompt += "No summary data available.\n\n"
    
    prompt += """================================================================================
GOOGLE SEARCH CONSOLE - TOP SEARCH QUERIES
================================================================================
Query | Clicks | Impressions | CTR | Position
"""
    
    rows = gsc_data.get('rows', [])
    if rows:
        for row in rows:
            query = row['keys'][0]
            clicks = row.get('clicks', 0)
            impressions = row.get('impressions', 0)
            ctr = row.get('ctr', 0)
            position = row.get('position', 0)
            prompt += f"{query} | {clicks:,.0f} | {impressions:,.0f} | {ctr:.2%} | {position:.1f}\n"
    else:
        prompt += "No query data available.\n"
    
    prompt += """
================================================================================
GOOGLE SEARCH CONSOLE - TOP PAGES
================================================================================
Page | Clicks | Impressions | CTR | Position
"""
    
    if gsc_pages and 'error' not in gsc_pages[0]:
        for row in gsc_pages:
            page = row['keys'][0]
            clicks = row.get('clicks', 0)
            impressions = row.get('impressions', 0)
            ctr = row.get('ctr', 0)
            position = row.get('position', 0)
            prompt += f"{page} | {clicks:,.0f} | {impressions:,.0f} | {ctr:.2%} | {position:.1f}\n"
    else:
        prompt += "No page data available.\n"
    
    prompt += """
================================================================================
GOOGLE ANALYTICS 4 - OVERALL METRICS
================================================================================
"""
    
    overall = ga4_data.get('overall', [])
    if overall and 'error' not in overall[0]:
        row = overall[0]
        metrics = row.get('metricValues', [])
        metric_names = ['Sessions', 'Total Users', 'Pageviews', 'Bounce Rate', 'Avg Session Duration', 'New Users']
        for i, name in enumerate(metric_names):
            if i < len(metrics):
                value = metrics[i].get('value', 'N/A')
                prompt += f"{name}: {value}\n"
    else:
        prompt += "No overall metrics available.\n"
    
    prompt += """
================================================================================
GOOGLE ANALYTICS 4 - TOP PAGES
================================================================================
Page Title (Path) | Sessions | Users | Pageviews | Bounce Rate | Avg Duration
"""
    
    pages = ga4_data.get('pages', [])
    if pages and 'error' not in pages[0]:
        for row in pages:
            dims = row.get('dimensionValues', [])
            mets = row.get('metricValues', [])
            title = dims[0].get('value', 'N/A') if len(dims) > 0 else 'N/A'
            path = dims[1].get('value', 'N/A') if len(dims) > 1 else 'N/A'
            sessions = mets[0].get('value', 'N/A') if len(mets) > 0 else 'N/A'
            users = mets[1].get('value', 'N/A') if len(mets) > 1 else 'N/A'
            pageviews = mets[2].get('value', 'N/A') if len(mets) > 2 else 'N/A'
            bounce = mets[3].get('value', 'N/A') if len(mets) > 3 else 'N/A'
            duration = mets[4].get('value', 'N/A') if len(mets) > 4 else 'N/A'
            prompt += f"{title} ({path}) | {sessions} | {users} | {pageviews} | {bounce} | {duration}s\n"
    else:
        prompt += "No page data available.\n"
    
    prompt += """
================================================================================
GOOGLE ANALYTICS 4 - TRAFFIC SOURCES
================================================================================
Channel | Sessions | Users
"""
    
    sources = ga4_data.get('sources', [])
    if sources and 'error' not in sources[0]:
        for row in sources:
            dims = row.get('dimensionValues', [])
            mets = row.get('metricValues', [])
            channel = dims[0].get('value', 'N/A') if len(dims) > 0 else 'N/A'
            sessions = mets[0].get('value', 'N/A') if len(mets) > 0 else 'N/A'
            users = mets[1].get('value', 'N/A') if len(mets) > 1 else 'N/A'
            prompt += f"{channel} | {sessions} | {users}\n"
    else:
        prompt += "No traffic source data available.\n"
    
    prompt += """
================================================================================
ANALYSIS INSTRUCTIONS FOR CLAUDE
================================================================================
Please analyze the data above and answer my question. Your response should:

1. Directly answer the specific question I asked
2. Provide data-backed insights and observations
3. Identify patterns, trends, or anomalies in the data
4. Offer specific, actionable recommendations
5. If relevant, compare GSC and GA4 data to find correlations
6. Highlight any opportunities for improvement
7. Be thorough but concise - use bullet points and clear sections

If the data is insufficient to fully answer the question, please say so and suggest what additional data would be helpful.
"""
    
    return prompt

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/generate', methods=['POST'])
def generate():
    # Get project details from form
    project_name = request.form.get('project_name', 'My Project')
    gsc_url = request.form.get('gsc_url', '').strip()
    ga4_property_id = request.form.get('ga4_property_id', '').strip()
    question = request.form.get('question', '')
    days = int(request.form.get('date_range', 30))
    
    # Validate inputs
    if not gsc_url:
        return render_template('error.html',
            error_title='GSC URL Required',
            error_message='Please enter your Google Search Console site URL.',
            back_url='/'
        )
    
    if not ga4_property_id:
        return render_template('error.html',
            error_title='GA4 Property ID Required',
            error_message='Please enter your GA4 Property ID.',
            back_url='/'
        )
    
    # Get credentials from service account
    creds = get_credentials()
    if not creds:
        return render_template('error.html',
            error_title='Service Account Not Configured',
            error_message='The GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set. Please add your service account JSON key to the environment variables in Render.',
            back_url='/'
        )
    
    # Fetch data
    gsc_data = fetch_gsc_data(creds, gsc_url, days)
    gsc_pages = fetch_gsc_pages(creds, gsc_url, days)
    ga4_data = fetch_ga4_data(creds, ga4_property_id, days)
    
    # Generate prompt
    prompt = generate_claude_prompt(project_name, gsc_url, ga4_property_id, question, gsc_data, gsc_pages, ga4_data, days)
    
    project = {
        'name': project_name,
        'gsc_url': gsc_url,
        'ga4_property_id': ga4_property_id
    }
    
    return render_template('prompt.html', 
                         prompt=prompt, 
                         project=project,
                         question=question,
                         days=days)

@app.route('/healthz')
def healthz():
    return {'status': 'ok'}, 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
