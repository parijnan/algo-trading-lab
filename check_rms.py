import os
import pandas as pd
from pyotp import TOTP
from SmartApi import SmartConnect
from datetime import datetime
import json

# ---------------------------------------------------------------------------
# Credentials Loading
# ---------------------------------------------------------------------------
REPO_ROOT      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR       = os.path.join(REPO_ROOT, "data")
CREDENTIALS_FILE = os.path.join(DATA_DIR, "user_credentials.csv")

def capture_analysis_data():
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"Error: {CREDENTIALS_FILE} not found.")
        return

    print("Loading credentials...")
    creds = pd.read_csv(CREDENTIALS_FILE).iloc[0]
    api_key = creds['api_key']
    user_name = creds['user_name']
    password = str(creds['password'])
    qr_code = creds['qr_code']

    print(f"Logging in as {user_name}...")
    obj = SmartConnect(api_key=api_key)
    try:
        totp = TOTP(qr_code).now()
        login_data = obj.generateSession(user_name, password, totp)
        if not login_data['status']:
            print(f"Login failed: {login_data['message']}")
            return
        print("Login successful.")
    except Exception as e:
        print(f"Exception during login: {e}")
        return

    analysis_result = {
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "rms": None,
        "orderbook": None
    }

    print("\nFetching RMS Limit Data...")
    try:
        rms_response = obj.rmsLimit()
        if rms_response['status']:
            analysis_result["rms"] = rms_response['data']
            print("RMS Data retrieved.")
            for key in sorted(rms_response['data'].keys()):
                print(f"  {key:25} : {rms_response['data'][key]}")
        else:
            print(f"Failed to fetch RMS: {rms_response['message']}")
    except Exception as e:
        print(f"Exception during RMS fetch: {e}")

    print("\nFetching Order Book...")
    try:
        ob_response = obj.orderBook()
        if ob_response['status']:
            analysis_result["orderbook"] = ob_response['data']
            print(f"Order Book retrieved. Total orders today: {len(ob_response['data'])}")
            
            # Print a quick summary of the last 10 orders
            print("\nLast 10 Orders Summary:")
            print(f"{'Time':10} | {'Symbol':20} | {'Type':5} | {'Qty':5} | {'Status':10}")
            for ord in ob_response['data'][-10:]:
                print(f"{ord['updatetime'][-8:]} | {ord['tradingsymbol']:20} | {ord['transactiontype']:5} | {ord['quantity']:5} | {ord['status']:10}")
        else:
            print(f"Failed to fetch Order Book: {ob_response['message']}")
    except Exception as e:
        print(f"Exception during Order Book fetch: {e}")

    # Save to analysis file
    output_file = os.path.join(DATA_DIR, f"debug_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(output_file, 'w') as f:
        json.dump(analysis_result, f, indent=4)
    print("\n" + "="*40)
    print(f"Full analysis data saved to: {output_file}")
    print("="*40)

if __name__ == "__main__":
    capture_analysis_data()
