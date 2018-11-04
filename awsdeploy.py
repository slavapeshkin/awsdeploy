from enum import Enum
import sys
import time
import json
import functools
import zipfile
import os
import logging
import boto3
import unittest
from typing import Dict, List, Any

logger = logging.getLogger()

DEFAULTS_RUN_UNIT_TESTS             = True
DEFAULTS_MAKE_PACKAGES              = True
DEFAULTS_UPLOAD_PACKAGES            = True
DEFAULTS_CREATE_STACKS              = True
DEFAULTS_COLLECT_STACK_OUTPUTS      = True
DEFAULTS_UPLOAD_STATIC_ARTIFACTS    = False

COMPILED_PYTHON_EXTENSION               = ".pyc"
ZIPFILE_EXTENSION                       = ".zip"
INIT_FILENAME                           = "__init__.py"
PYSRC_DIRS_ALWAYS_EXCLUDE               = ["tests", "__pycache__"]
AWS_CAPABILITY_IAM                      = "CAPABILITY_IAM"
AWS_CLOUDFORMATION_WAITER_CONFIG        = { "Delay": 30, "MaxAttempts": 50 }
AWS_COLLECT_OUTPUTS_CONFIG              = { "Timeout": 50, "SleepSeconds": 5 }
AWS_CLOUDFORMATION_CREATE_COMPLETE      = "CREATE_COMPLETE"
AWS_CLOUDFORMATION_CREATE_IN_PROGRESS   = "CREATE_IN_PROGRESS"


class Status(Enum):
    OK      = 1
    FAILED  = 2
    SKIPPED = 3


class DeploymentStep(object):

    def __init__(self, name: str, func: Any, args: List = None, kwargs: Dict = None) -> None:
        self.name   = name
        self.func   = func
        self.args   = args or []
        self.kwargs = kwargs or {}

    def apply(self) -> Status:
        return self.func(*self.args, **self.kwargs)


class AwsDeployExpert(object):

    def __init__(self, config: Dict) -> None:
        self.config     = config
        self.cf_client  = None
        self.s3_client  = None
        self.state      = {}

    def deploy(self) -> Status:
        """ Starts deployment pipeline """
        logger.info("Starting deployment pipeline")
        for step in self._get_deploy_steps():
            logger.info("Applying step %s", step.name)
            status = step.apply()
            logger.info("Step %s returned %s", step.name, status)
            if status == Status.FAILED:
                logger.error("step %s failed. Terminated", step.name)
                break
        logger.info("Finished deployment pipeline")
        return Status.OK

    def _get_deploy_steps(self) -> List:
        """ Returns a list of deployment steps which will be performed """
        steps = [
            DeploymentStep('run_tests', self._run_tests),
            DeploymentStep('make_packages', self._make_packages),
            DeploymentStep('init_aws', self._init_aws),
            DeploymentStep('upload_packages_to_s3', self._upload_packages_to_s3_bucket),
            DeploymentStep('create_stacks', self._create_stacks),
            DeploymentStep('collect_stack_outputs', self._collect_stack_outputs),
            DeploymentStep('upload_static_artifacts', self._upload_static_artifacts),
        ]
        return steps

    def _run_tests(self) -> Status:
        """ Runs unit tests """
        if not self.config.get("options",{}).get("runUnitTests", DEFAULTS_RUN_UNIT_TESTS):
            return Status.SKIPPED
        sourcePath = self.config.get("sourcePath", None)
        if not sourcePath:
            logger.error("missing sourcePath")
            return Status.FAILED
        passed = runUnitTests(sourcePath)
        return Status.OK if passed else Status.FAILED

    def _make_packages(self) -> Status:
        """ Makes python source packages and includes dependent libraries """
        if not self.config.get("options",{}).get("makePackages", DEFAULTS_MAKE_PACKAGES):
            return Status.SKIPPED
        sourcePath  = self.config.get("sourcePath", None)
        libPath     = self.config.get("libPath", None)
        if not sourcePath:
            logger.error("missing sourcePath")
            return Status.FAILED
        status = Status.OK
        for package in self.config.get("packages",[]):
            packageName = package.get("name",None)
            response    = makePySrcPackage( zipPackageName  = packageName,
                                            pySrcPath       = sourcePath,
                                            excludeDirs     = package.get( "sourceDirsToExclude", [] ),
                                            addInit         = package.get( "addInitAtRoot", False ) )
            logger.info("Created package. Response: '%s'", response)
            if not response:
                status = Status.FAILED
                break
            if libPath:
                libsToInclude = package.get("libsToInclude",[])
                libsToExclude = package.get("libsToExclude",[])
                status = addPackageLibs(packageName, libPath, libsToExclude, libsToInclude)
                if status == Status.FAILED:
                    break
                logger.info("Added libraries to package %s. Response: '%s'", packageName, response)
        return status

    def _init_aws(self) -> Status:
        """ Initialized AWS Env vars and boto3 clients """
        profile = self.config.get("aws", {}).get("awsProfile", None)
        if profile:
            logger.info("setting AWS_PROFILE=%s", profile)
            os.environ["AWS_PROFILE"] = str(profile)
        else:
            logger.info("using AWS_PROFILE '%s'", os.environ.get("AWS_PROFILE", None))
        self.cf_client = boto3.client('cloudformation')
        self.s3_client = boto3.client('s3')
        return Status.OK

    def _upload_packages_to_s3_bucket(self) -> Status:
        """ Uploads packages to s3 source bucket """
        if not self.config.get("options",{}).get("uploadPackages", DEFAULTS_UPLOAD_PACKAGES):
            return Status.SKIPPED
        for package in self.config.get("packages", []):
            packageName = package.get("name", None)
            srcS3Bucket = package.get("aws", {}).get("srcS3Bucket", None)
            srcS3Key    = package.get("aws", {}).get("srcS3Key", None)
            if not(packageName and srcS3Bucket and srcS3Key):
                logger.error( "missing packageName or bucket or key; ('%s', '%s', '%s')", packageName,srcS3Bucket, srcS3Key )
                return Status.FAILED
            response = uploadFileToS3Bucket(self.s3_client, packageName, srcS3Bucket, srcS3Key)
            logger.info("Uploaded package '%s' to S3 source bucket. Response: %s", packageName, response)
        return Status.OK

    def _create_stacks(self) -> Status:
        """ Executes CloudFormation templates and creates stacks """
        if not self.config.get("options",{}).get("createStacks", DEFAULTS_CREATE_STACKS):
            return Status.SKIPPED
        for stack in self.config.get("stacks", []):
            stackName           = stack.get("name", None)
            templatePath        = stack.get("templatePath", None)
            templateParamsPath  = stack.get("templateParamsPath", None)
            region              = stack.get("region", None)
            if not(stackName and templatePath and region):
                logger.error( "missing stackName or templatePath or region; ('%s', '%s', '%s')", stackName and templatePath and region)
                return Status.FAILED
            with open(templatePath, 'r') as f:
                templateBody = f.read()
            parameters = []
            if templateParamsPath:
                with open(templateParamsPath, 'r') as f:
                    parameters.extend( json.loads(f.read()) )
            parameters.extend(stack.get("params",{}))
            logger.info("Started CloudFormation create stack for '%s'", stackName)
            stackId = createStack(self.cf_client, stackName, templateBody, parameters, region)
            logger.info("StackId '%s' is '%s'", stackName, stackId)
            waitCreateStackComplete(self.cf_client, stackName)

    def _collect_stack_outputs(self) -> Status:
        """ Collects stack outputs and persists in the state. Returns status"""
        if not self.config.get("options",{}).get("collectStackOutputs", DEFAULTS_COLLECT_STACK_OUTPUTS):
            return Status.SKIPPED
        self.state["stacks"] = {}
        for stack in self.config.get("stacks", []):
            stackName   = stack.get("name", None)
            outputs     = getStackOutputs(self.cf_client, stackName)
            self.state["stacks"].update({stackName: {"outputs": outputs}})
        return Status.OK

    def _upload_static_artifacts(self) -> Status:
        """ Post deploy step to upload static artifacts to s3 bucket which was created with the stack """
        if not self.config.get("options",{}).get("uploadStaticArtifacts", DEFAULTS_UPLOAD_STATIC_ARTIFACTS):
            return Status.SKIPPED
        for artifact in self.config.get("staticArtifacts", []):
            staticPath              = artifact.get("staticPath", None)
            stackNameForS3Bucket    = artifact.get("stackNameForS3Bucket", None)
            outputKeyForS3Bucket    = artifact.get("outputKeyForS3Bucket", None)
            if not (staticPath and stackNameForS3Bucket and outputKeyForS3Bucket):
                logger.error("missing staticPath or stackNameForS3Bucket or outputKeyForS3Bucket; ('%s', '%s', '%s')",
                             staticPath and stackNameForS3Bucket and outputKeyForS3Bucket)
                return Status.FAILED
            outputs = self.state.get("stacks",{}).get(stackNameForS3Bucket,{}).get("outputs",None)
            if not outputs:
                logger.info("No outputs found for stack '%s'", stackNameForS3Bucket)
                continue
            staticS3Bucket = [x["OutputValue"] for x in outputs if x["OutputKey"] == outputKeyForS3Bucket][0].split(":")[-1]
            logger.info("Uploading static artifacts from '%s' to bucket %s", staticPath, staticS3Bucket)
            uploadDirectoryToS3Bucket(self.s3_client, staticPath, staticS3Bucket)
        return Status.OK


def runUnitTests( path: str ) -> bool:
    """ Loads and runs tests from source path. Returns True if success """
    x = os.path.join(os.path.dirname(__file__), path)
    if x not in sys.path:
        sys.path.append(x)
    loader = unittest.TestLoader()
    suite = loader.discover(path)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return len(result.errors) == 0 and len(result.failures) == 0

def makePySrcPackage( zipPackageName: str, pySrcPath: str, excludeDirs = None, addInit = False ) -> str:
    def _filterFunc(root_, file_, excludeDirs_):
        foldersToSkip = PYSRC_DIRS_ALWAYS_EXCLUDE + excludeDirs_
        for fldName in foldersToSkip:
            if fldName in root_:
                return False
        if str(file_).endswith(COMPILED_PYTHON_EXTENSION):
            return False
        return True
    filterFunc = functools.partial(_filterFunc, excludeDirs_=excludeDirs or [])
    with zipfile.ZipFile(zipPackageName,'w') as zh:
        for root, dirs, files in os.walk(pySrcPath):
            for file in files:
                if filterFunc(root, file):
                    arcname = os.path.join(root.replace(pySrcPath,''), file)
                    zh.write(os.path.join(root, file),arcname=arcname, compress_type=zipfile.ZIP_DEFLATED)
                    #zh.write(os.path.join(root, file) ,arcname=file)
        if addInit:
            zh.write(os.path.join(pySrcPath, INIT_FILENAME),arcname=INIT_FILENAME)
    return zipPackageName

def addPackageLibs( zipPackageName: str, pyLibsPath: str, excludeLibs = None, includeLibs = None ) -> Status:
    for root, dirs, files in os.walk(pyLibsPath):
        for file in files:
            if not file.endswith(ZIPFILE_EXTENSION):
                continue
            if (includeLibs and file not in includeLibs) or (excludeLibs and file in excludeLibs):
                logger.info("Skipped lib '%s' ", file)
                continue
            zipFileAppendFrom = os.path.join(root, file)
            logger.info("Adding lib '%s' to package '%s'", zipFileAppendFrom, zipPackageName)
            appendZipToZip( zipPackageName, zipFileAppendFrom )
    return Status.OK

def appendZipToZip( zipFileAppendTo: str, zipFileAppendFrom: str ) -> Status:
    """ Appends contents from one zip file to another zip file """
    z1 = zipfile.ZipFile( zipFileAppendTo, 'a' )
    z2 = zipfile.ZipFile( zipFileAppendFrom, 'r' )
    for t in ((n, z2.open(n)) for n in z2.namelist()):
        #z1.writestr(t[0], t[1].read(), compress_type=zipfile.ZIP_DEFLATED)
        zip_info = zipfile.ZipInfo(t[0])
        zip_info.compress_type = zipfile.ZIP_DEFLATED
        zip_info.create_system = 3  # Specifies Unix
        zip_info.external_attr = 0o777 << 16  # Sets chmod 777 on the file
        z1.writestr(zip_info, t[1].read(), compress_type=zipfile.ZIP_DEFLATED)
    z1.close()
    return Status.OK

def uploadFileToS3Bucket(s3_client: Any, filePath: str, s3Bucket: str, s3Key: str) -> str:
    """ Uploads file to s3 bucket using boto3 s3 client """
    return s3_client.upload_file(filePath, s3Bucket, s3Key)

def uploadDirectoryToS3Bucket(s3_client: Any, dirPath: str, s3Bucket: str) -> Status:
    for subdir, dirs, files in os.walk(dirPath):
        for fileName in files:
            full_path   = os.path.join(subdir, fileName)
            key         = full_path[len(dirPath):].replace("\\", "/")
            response    = uploadFileToS3Bucket( s3_client, full_path, s3Bucket, s3Key=key)
            logger.info("Uploaded '%s' to key '%s' in bucket %s. Response: %s", fileName, key,s3Bucket, response)
    return Status.OK

def createStack(cf_client: Any, stackName: str, templateBody: str, parameters: List, region: str)-> str:
    """ Returns StackId """
    response = cf_client.create_stack(
        StackName       = stackName,
        TemplateBody    = templateBody,
        Parameters      = parameters,
        Capabilities    = [ AWS_CAPABILITY_IAM, ]
    )
    return response

def waitCreateStackComplete(cf_client: Any, stackName: str) -> None:
    waiter = cf_client.get_waiter('stack_create_complete')
    waiter.wait( StackName=stackName, NextToken='string', WaiterConfig=AWS_CLOUDFORMATION_WAITER_CONFIG )

def getStackOutputs(cf_client: Any, stackName: str) -> Dict:
    """ Returns StackOutputs """
    timeoutSeconds  = AWS_COLLECT_OUTPUTS_CONFIG.get("Timeout", -1)
    sleepSeconds    = AWS_COLLECT_OUTPUTS_CONFIG.get("SleepSeconds", 1)
    elapsedSeconds  = 0
    stackOutputs    = None
    while elapsedSeconds < timeoutSeconds:
        stackDesc = describeStack(cf_client, stackName)
        status = stackDesc["Stacks"][0]["StackStatus"]
        if status == AWS_CLOUDFORMATION_CREATE_COMPLETE:
            stackOutputs = stackDesc["Stacks"][0]["Outputs"]
            break
        elif status == AWS_CLOUDFORMATION_CREATE_IN_PROGRESS:
            logger.info("Stack '%s' status is CREATE_IN_PROGRESS. sleeping %d seconds", stackName, sleepSeconds)
            elapsedSeconds += sleepSeconds
            time.sleep(sleepSeconds)
        else:
            raise stackDesc
    return stackOutputs

def describeStack(cf_client: Any, stackName):
    """ Returns Summary Info for created stack """
    return cf_client.describe_stacks( StackName = stackName )

