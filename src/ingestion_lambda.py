import json
import boto3
import requests
import time

# --- Configuration ---
# The name of the secret you created in AWS Secrets Manager
SECRET_NAME = "reddit/api_keys"
# The name of your Kinesis Data Firehose delivery stream
FIREHOSE_STREAM_NAME = "reddit-comment-stream"
# The subreddit you want to fetch comments from
TARGET_SUBREDDIT = "conservative"

# Initialize AWS clients
secrets_client = boto3.client('secretsmanager')
firehose_client = boto3.client('firehose')

def get_reddit_token(client_id, client_secret):
    """
    Authenticates with the Reddit API to get an access token.
    Reddit API requires a 'script' type app for this authentication flow.
    """
    auth = requests.auth.HTTPBasicAuth(client_id, client_secret)
    # You must provide a dummy username and password for the 'password' grant type
    # even though they are not used for script authentication.
    data = {
        'grant_type': 'client_credentials',
    }
    headers = {'User-Agent': 'MyRedditScraper/0.1'}
    
    try:
        res = requests.post('https://www.reddit.com/api/v1/access_token',
                            auth=auth, data=data, headers=headers)
        res.raise_for_status() # Raise an exception for bad status codes
        token_data = res.json()
        return token_data['access_token']
    except requests.exceptions.RequestException as e:
        print(f"Error getting Reddit token: {e}")
        return None

def lambda_handler(event, context):
    """
    Main Lambda function handler.
    Fetches secrets, gets a Reddit token, fetches new comments, and puts them into Firehose.
    """
    print("Fetching secrets from Secrets Manager...")
    try:
        secret_response = secrets_client.get_secret_value(SecretId=SECRET_NAME)
        secrets = json.loads(secret_response['SecretString'])
        
        client_id = secrets.get('reddit_client_id')
        client_secret = secrets.get('reddit_client_secret')
        user_agent = secrets.get('reddit_user_agent')
        
        if not all([client_id, client_secret, user_agent]):
            raise ValueError("One or more required secrets are missing.")

    except Exception as e:
        print(f"Failed to retrieve secrets: {e}")
        return {'statusCode': 500, 'body': json.dumps(f"Failed to retrieve secrets: {e}")}

    print("Authenticating with Reddit...")
    token = get_reddit_token(client_id, client_secret)
    
    if not token:
        print("Failed to get Reddit token. Exiting.")
        return {'statusCode': 500, 'body': json.dumps("Failed to get Reddit token.")}

    print("Fetching new comments from Reddit...")
    headers = {'Authorization': f'bearer {token}', 'User-Agent': user_agent}
    
    try:
        # Fetch the latest 100 comments from the target subreddit
        reddit_url = f'https://oauth.reddit.com/r/{TARGET_SUBREDDIT}/comments?limit=100'
        res = requests.get(reddit_url, headers=headers)
        res.raise_for_status()
        
        comments = res.json()['data']['children']
        print(f"Successfully fetched {len(comments)} comments.")
        
        records_to_send = []
        for comment in comments:
            comment_data = comment['data']
            # Create a record for Firehose. It must be bytes.
            # Adding a newline character is a best practice for separating JSON objects in a file.
            record = {
                'Data': (json.dumps(comment_data) + '\n').encode('utf-8')
            }
            records_to_send.append(record)
        
        # Send records to Firehose in a batch for efficiency
        if records_to_send:
            print(f"Sending {len(records_to_send)} records to Kinesis Firehose...")
            # PutRecordBatch can handle up to 500 records at a time
            # We split into chunks of 500 just in case we fetch more in the future
            for i in range(0, len(records_to_send), 500):
                chunk = records_to_send[i:i+500]
                result = firehose_client.put_record_batch(
                    DeliveryStreamName=FIREHOSE_STREAM_NAME,
                    Records=chunk
                )
                # Check for failed records and log them
                if result.get('FailedPutCount', 0) > 0:
                    print(f"Warning: {result['FailedPutCount']} records failed to be sent to Firehose.")
                    # You could add more robust error handling here
                
        return {
            'statusCode': 200,
            'body': json.dumps(f'Successfully processed and sent {len(records_to_send)} comments to Firehose.')
        }

    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch comments from Reddit: {e}")
        return {'statusCode': 500, 'body': json.dumps(f"Failed to fetch comments: {e}")}
    except Exception as e:
        print(f"An error occurred: {e}")
        return {'statusCode': 500, 'body': json.dumps(f"An error occurred: {e}")}

