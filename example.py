from awsdeploy import AwsDeployExpert, Status

def main():
    """
        your directory structure might look like this
        ---------------------------------------------
        lib/
            thirdpartylib1.zip
            thirdpartylib2.zip
        src/
            __init__.py
            tests/
                __init__.py
        static/
            index.html
            readme.txt
        templates/
            cloudformation-template.json
            cloudformation-template.parameters.dev.json

    """

    deploy_config = {

        "aws": {
            "awsProfile": "your_aws_profile",
        },

        "sourcePath"   : "src/",
        "libPath"      : "lib/",

        "options": {
            "runUnitTests"          : True,
            "makePackages"          : True,
            "uploadPackages"        : True,
            "createStacks"          : True,
            "collectStackOutputs"   : True,
            "uploadStaticArtifacts" : True,
        },

        "packages": [
            {
                "name": "package-name.zip",
                "sourceDirsToExclude": [],
                "libsToInclude": [],
                "libsToExclude": [],
                "addInitAtRoot" : False,
                "aws":{
                    "srcS3Bucket" : "your-s3-source-bucket",
                    "srcS3Key"    : "package-key-in-your-s3-source-bucket",
                }
            }
        ],

        "stacks": [
            {
                "name"                  : "your-stack-1",
                "templatePath"          : "templates/cloudformation-template.json",
                "templateParamsPath"    : "templates/cloudformation-template.parameters.dev.json",
                "params"                : [],
                "region"                : "pick-your-region"
            }

        ],

        "staticArtifacts": [
            {
                "staticPath"            : "static/",
                "stackNameForS3Bucket"  : "your-stack-1",
                "outputKeyForS3Bucket"  : "BucketCreatedInStackBucketArn"
            }

        ]

    }

    expert = AwsDeployExpert(deploy_config)
    status = expert.deploy()
    return 0 if status == Status.OK else 1

if __name__ == "__main__":
   main()
