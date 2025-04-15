from flask import Flask, request, render_template
from instaloader import Instaloader, Profile, exceptions
from urllib.parse import urlparse
import gspread
from google.oauth2.service_account import Credentials
import time
import random
import os  # Import the 'os' module for file operations

scope = ['https://www.googleapis.com/auth/spreadsheets.readonly']
creds = Credentials.from_service_account_file('insta-scrape-prof-url-f24e7e8814ed.json', scopes=scope)
gc = gspread.authorize(creds)

app = Flask(__name__)

# --- Google Sheets Configuration ---
CREDENTIALS_FILE = "insta-scrape-prof-url-f24e7e8814ed.json"
SPREADSHEET_NAME = 'My Instagram Data'
CREDENTIALS_WORKSHEET = 'Account Credentials'
USERNAME_COL = 0
PASSWORD_COL = 1

# --- Instaloader Configuration ---
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:95.0) Gecko/20100101 Firefox/95.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:95.0) Gecko/20100101 Firefox/95.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.45 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Safari/605.1.15",
]

# --- Retry and Delay Configuration ---
MAX_LOGIN_RETRIES = 3
INITIAL_LOGIN_DELAY = 10
MAX_LOGIN_DELAY = 60
SCRAPE_DELAY_RANGE = (15, 45)

# --- Global Variables ---
urls_to_scrape = []
urls_processed_with_current_account = 0
urls_per_account_limit = 22
current_account_index = 0
scraping_in_progress = False
current_loader = None  # To store the Instaloader instance for the current account
current_username = None
SESSION_FILE_PREFIX = "session-"  # Standard Instaloader session file prefix
scraped_data_queue = [] # Use a queue to handle multiple results

def get_credentials_from_sheet():
    try:
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key('1pEmMrAw_PuevwYNWLwUPANz_MdV9PZODKUkuIwGR7Jg')
        print("Successfully opened spreadsheet!")
        worksheet = spreadsheet.worksheet(CREDENTIALS_WORKSHEET)
        credentials_list = worksheet.get_all_values()
        print(f"Retrieved credentials list: {credentials_list}")
        return credentials_list[0:]
    except gspread.exceptions.WorksheetNotFound as e:
        print(f"Error: Worksheet '{CREDENTIALS_WORKSHEET}' not found: {e}")
        return None
    except Exception as e:
        print(f"Error retrieving credentials from Google Sheets: {e}")
        return None


def get_username_from_url(profile_url):
    parsed_url = urlparse(profile_url)
    path_segments = parsed_url.path.strip('/').split('/')
    if path_segments:
        return path_segments[0]
    return None


def scrape_profile_data(loader, profile_url, username):
    try:
        username_to_scrape = get_username_from_url(profile_url)
        if not username_to_scrape:
            print(f"Error scraping with {username}: Could not extract username from URL '{profile_url}'.")
            return {"account": username, "error": "Invalid Profile URL"}

        profile = Profile.from_username(loader.context, username_to_scrape)
        data = {
            "username": profile.username,
            "followers": profile.followers,
            "following": profile.followees,
            "total_posts": profile.mediacount,
            "private": profile.is_private,
        }
        print(f"Scraped data with {username} for '{profile_url}': {data}")
        return data
    except exceptions.ProfileNotExistsException:
        print(f"Error scraping with {username}: Profile '{username_to_scrape}' not found.")
        return {"error": f"Profile '{username_to_scrape}' not found"}
    except Exception as e:
        print(f"Error scraping with {username} for '{profile_url}': {e}")
        return {"error": str(e)}


def attempt_login(loader, username, password):
    for attempt in range(MAX_LOGIN_RETRIES):
        try:
            loader.login(username, password)
            print(f"Successfully logged in as {username}")
            return True
        except exceptions.ConnectionException as e:
            print(f"Connection error during login for {username}: {e}")
        except exceptions.InvalidArgumentException as e:
            print(f"Invalid argument during login for {username}: {e}")
            return False
        except exceptions.LoginException as e:
            if "Checkpoint required" in str(e):
                print(f"Checkpoint required for {username}. Manual intervention needed: {e}")
                return False
            else:
                print(f"Login error for {username}: {e}")
                return False
        except Exception as e:
            print(f"An unexpected error occurred during login for {username}: {e}")
            return False
        if attempt < MAX_LOGIN_RETRIES - 1:
            time.sleep(min(INITIAL_LOGIN_DELAY * (2 ** attempt), MAX_LOGIN_DELAY))
    return False


def logout_account(username):
    session_file = f"{SESSION_FILE_PREFIX}{username}"
    try:
        if os.path.exists(session_file):
            os.remove(session_file)
            print(f"Successfully removed session file for {username}, effectively logging out.")
        else:
            print(f"No session file found for {username}.")
    except Exception as e:
        print(f"Error deleting session file for {username}: {e}")


@app.route('/', methods=['GET'])
def index():
    return render_template('i2.html', scraped_data=None, message=None, error=None)


@app.route('/scrape', methods=['POST'])
def scrape_process():
    global current_account_index, scraping_in_progress, urls_to_scrape, urls_processed_with_current_account, urls_per_account_limit, current_loader, current_username, scraped_data_queue

    profile_url = request.form.get('profileUrl')
    if not profile_url:
        return render_template('i2.html', error="Please enter a profile URL.", scraped_data=None, message=None)

    urls_to_scrape.append(profile_url)

    credentials_list = get_credentials_from_sheet()
    if not credentials_list:
        return render_template('i2.html', error="Could not retrieve Instagram credentials.", scraped_data=None, message=None)

    if current_account_index < len(credentials_list):
        username, password = credentials_list[current_account_index]

        if not scraping_in_progress or current_username != username:
            scraping_in_progress = True
            current_username = username
            current_loader = Instaloader()
            current_loader.context._session.headers['User-Agent'] = random.choice(USER_AGENTS)
            current_loader.max_connection_attempts = 3

            logged_in = attempt_login(current_loader, username, password)
            if not logged_in:
                logout_account(username)
                current_account_index += 1
                urls_processed_with_current_account = 0
                scraping_in_progress = False
                current_loader = None
                current_username = None
                return render_template('i2.html', error=f"Failed to log in with account {username}. Moving to the next account.", scraped_data=None, message=None)
            else:
                urls_processed_with_current_account = 0 # Reset count after successful (re)login

        if urls_processed_with_current_account < urls_per_account_limit and urls_to_scrape:
            url_to_process = urls_to_scrape.pop(0)
            data = scrape_profile_data(current_loader, url_to_process, username)
            urls_processed_with_current_account += 1
            print(f"Scraped {urls_processed_with_current_account} URLs with account {username}")
            return render_template('i2.html', scraped_data=data, message=None, error=None)

        if urls_processed_with_current_account >= urls_per_account_limit or not urls_to_scrape:
            logout_account(username)
            current_account_index += 1
            urls_processed_with_current_account = 0
            scraping_in_progress = False
            current_loader = None
            current_username = None
            message = f"Processed {urls_processed_with_current_account} URLs with account {username}. Moving to the next account." if urls_processed_with_current_account > 0 else f"No URLs processed with account {username}. Moving to the next account."
            return render_template('i2.html', message=message, scraped_data=None, error=None)

        return render_template('i2.html', message="Waiting for more URLs to process with the current account.", scraped_data=None, error=None)

    else:
        scraping_in_progress = False
        current_account_index = 0
        urls_to_scrape = []
        urls_processed_with_current_account = 0
        current_loader = None
        current_username = None
        return render_template('i2.html', message="All accounts have been used for the current batch of URLs.", scraped_data=None, error=None)


if __name__ == '__main__':
    app.run(debug=True)