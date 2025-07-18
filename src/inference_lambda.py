import json
import boto3
import urllib.parse
import time

# --- Configuration ---
# The name of your SageMaker Serverless endpoint
SAGEMAKER_ENDPOINT_NAME = "fb-hatespeech-reddit" 
# The name of your DynamoDB table
DYNAMODB_TABLE_NAME = "reddit_comment_analysis"

# Initialize AWS clients
s3_client = boto3.client('s3')
sagemaker_runtime_client = boto3.client('sagemaker-runtime')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(DYNAMODB_TABLE_NAME)

def lambda_handler(event, context):
    """
    This function is triggered by an S3 event.
    It reads the new file, invokes the SageMaker endpoint for each comment,
    and stores the result in DynamoDB.
    """
    print("Received event:", json.dumps(event))

    # 1. Get the bucket and file key from the S3 event notification
    try:
        bucket = event['Records'][0]['s3']['bucket']['name']
        # The key may have URL-encoded characters (e.g., spaces as '+')
        key = urllib.parse.unquote_plus(event['Records'][0]['s3']['object']['key'], encoding='utf-8')
        print(f"Processing file: s3://{bucket}/{key}")
    except KeyError as e:
        print("Error: Could not parse S3 event.")
        print(e)
        return {'statusCode': 400, 'body': 'Malformed S3 event'}

    # 2. Get the file content from S3
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        # The file from Firehose is a stream of JSON objects, one per line
        content = response['Body'].read().decode('utf-8')
    except Exception as e:
        print(f"Error getting file from S3: {e}")
        return {'statusCode': 500, 'body': f'Error getting file from S3: {e}'}

    # 3. Process each line (each comment) in the file
    processed_count = 0
    for line in content.splitlines():
        if not line.strip():
            continue
        
        try:
            comment_data = json.loads(line)
            comment_id = comment_data.get('id')
            comment_body = comment_data.get('body')

            if not comment_id or not comment_body:
                print("Skipping malformed comment record:", line)
                continue

            # 4. Invoke the SageMaker Serverless endpoint
            sagemaker_response = sagemaker_runtime_client.invoke_endpoint(
                EndpointName=SAGEMAKER_ENDPOINT_NAME,
                ContentType='application/json',
                Body=json.dumps({"inputs": comment_body})
            )
            
            # The response body is a stream, read and parse it
            result_str = sagemaker_response['Body'].read().decode('utf-8')
            result_json = json.loads(result_str)
            
            # Assuming the model returns a list like [{'label': 'hate', 'score': 0.99}]
            prediction = result_json[0]
            label = prediction.get('label')
            score = prediction.get('score')

            # 5. Store the results in DynamoDB
            table.put_item(
                Item={
                    'comment_id': comment_id,
                    'comment_body': comment_body,
                    'subreddit': comment_data.get('subreddit'),
                    'author': comment_data.get('author'),
                    'prediction_label': label,
                    # DynamoDB doesn't handle float types well, so convert score to a string or Decimal
                    'prediction_score': str(round(score, 4)) if score is not None else None,
                    'created_utc': int(comment_data.get('created_utc', 0))
                }
            )
            processed_count += 1

             # --- THE FIX ---
            # Add a small delay to prevent throttling the SageMaker endpoint
            time.sleep(0.1) 

        except Exception as e:
            print(f"Error processing a comment record: {e}")
            print("Problematic line:", line)
            # Continue to the next line instead of failing the whole function
            continue
    
    print(f"Successfully processed and stored {processed_count} comments.")
    return {
        'statusCode': 200,
        'body': json.dumps(f'Successfully processed {processed_count} comments from {key}')
    }
