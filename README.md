# awsdeploy
**AwsDeployExpert** helps to automate a number of routine deployment steps. 

It will be particularly useful for serverless deployment. 

Expert can do the following:
1. Discover and run your unit tests. Terminate if any of the tests fail
2. Assemble your lambda python sources into packages
3. Enrich assembled source packages with third-party libraries (i.e. if you have pypi dependencies)
4. Upload packages to S3 'source' bucket
5. Create CloudFormation stacks and collect outputs which can be used for post-deploy steps
6. Upload static artifacts into newly created bucket

Suggestions welcome
