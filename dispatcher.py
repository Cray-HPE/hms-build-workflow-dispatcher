#!/usr/bin/env python3

# MIT License
#
# (C) Copyright [2022] Hewlett Packard Enterprise Development LP
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

import collections
import copy
from datetime import datetime, timedelta
import glob
import json
import logging
import os
import re
import shutil
import tarfile
import time
from urllib.parse import urljoin

from deepdiff import DeepDiff
from git import Repo
from github import Github
import requests
import yaml
import subprocess

def GetDockerImageFromDiff(value, tag):
    # example: root['artifactory.algol60.net/csm-docker/stable']['images']['hms-trs-worker-http-v1'][0]
    values = value.split(']')
    image = values[2]
    image = image.replace('[', '')
    image = image.replace('\'', '')
    return 'artifactory.algol60.net/csm-docker/stable/' + image + ':' + tag


def FindImagePart(value):
    # example: root['artifactory.algol60.net/csm-docker/stable']['images']['hms-trs-worker-http-v1'][0]
    replace0 = "root['artifactory.algol60.net/csm-docker/stable']['images']['"
    replace1 = "'][0]"
    value = value.replace(replace0, '')
    return value.replace(replace1, '')

def CreateJobSummaryTemplateValues(rebuilt_images, summary):
    template_values = {}
    template_values["repos"] = []
    # template_values["summary"] = summary["summary"]

    for github_repo, images in rebuilt_images.items():
        image_list = []
        for image in images:

            # TODO/HACK grab the first execution if it exists
            job_url = None
            workflow_name = None
            job_status = ":grey_question:"

            if len(image["executions"]) > 0:
                job_url = image["executions"][0].get("job-html-url", None)
                workflow_name = image["executions"][0].get("workflow-name", None)
                job_status = image["executions"][0].get("job-status", None)

                if job_status == "completed":
                    job_status = ":white_check_mark:"
                elif job_status == "failure":
                    job_status = ":x:"


            image_list.append({
                "git_repo": github_repo,
                "full_image": image["full-image"],
                "short_name": image["short-name"],
                "image_tag": image["image-tag"],
                "git_tag": image["git-tag"],
                "csm_releases": image["csm-releases"],
                "job_status": job_status,
                "job_url": job_url,
                "workflow_name": workflow_name
            })

        # Sort first by the repo, then by git tag, and last the image name 
        image_list.sort(key=lambda e: (e["git_repo"], e["git_tag"], e["full_image"]))

        template_values["repos"].append({
            "git_repo": github_repo,
            "images": image_list
        })

    # Sort by repo
    template_values["repos"].sort(key=lambda e: (e["git_repo"]))
        
    return template_values

if __name__ == '__main__':

    ####################
    # Load Configuration
    ####################

    github_token = os.getenv("GITHUB_TOKEN")

    with open("configuration.yaml") as stream:
        try:
            config = yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            logging.error(exc)
            exit(1)

    g = Github(github_token)

    sleep_duration = os.getenv('SLEEP_DURATION_SECONDS', config["configuration"]["sleep-duration-seconds"])
    expiration_minutes = os.getenv('TIME_LIMIT_MINUTES', config["configuration"]["time-limit-minutes"])
    webhook_sleep_seconds = os.getenv('WEBHOOK_SLEEP_SECONDS', config["configuration"]["webhook-sleep-seconds"])
    log_level = os.getenv('LOG_LEVEL', config["configuration"]["log-level"])

    logging.basicConfig(level=log_level)
    logging.info("load configuration")

    dry_run = False
    if os.getenv("DRYRUN", "false").lower() == "true":
        logging.info("Performing a dry run!")
        dry_run = True

    ####################
    # Download the CSM repo
    ####################
    logging.info("retrieve manifest repo")

    csm = config["configuration"]["manifest-repo"]
    csm_repo_metadata = g.get_organization("Cray-HPE").get_repo(csm)
    csm_dir = csm
    # Clean up in case it exsts
    if os.path.exists(csm_dir):
        shutil.rmtree(csm_dir)

    os.mkdir(csm_dir)
    csm_repo = Repo.clone_from(csm_repo_metadata.clone_url, csm_dir)

    ####################
    # Go Get LIST of Docker Images we need to investigate!
    ####################
    logging.info("find docker images")
    images_to_rebuild = {}

    docker_image_tuples = []
    for branch in config["configuration"]["targeted-csm-branches"]:
        logging.info("Checking out CSM branch {} for docker image extraction".format(branch))

        csm_repo.git.checkout(branch)

        # load the docker index file
        docker_index = os.path.join(csm_dir, config["configuration"]["docker-image-manifest"])
        with open(docker_index) as stream:
            try:
                manifest = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                logging.error(exc)
                exit(1)

        compare = config["docker-image-compare"]

        ############################
        # THis is some brittle logic!
        ############################
        # This ASSUMES that the docker/index.yaml file has no key depth greater than 3!
        # This ASSUMES that all images are in artifactory.algol60.net/csm-docker/stable
        # , it assume that '[' or ']' is part of the library, and NOT part of a legit value.
        # compare the two dictionaries, get the changed values only.  Since the compare file has 'find_me' baked into all
        # the values I care about, it should make it easier to find the actual image tags.
        # perhaps there is some easier way to do this. or cleaner? Maybe I should have just used YQ and hard coded a lookup list
        # I think it will be easier, cleaner if I provide a manual lookup between the image name and the repo in github.com\Cray-HPE;
        # otherwise id have to do a docker inspect of some sort, which seems like a LOT of work

        ddiff = DeepDiff(compare, manifest)
        changed = ddiff["values_changed"]
        docker_image_tuples = []
        for k, v in changed.items():
            path_to_digest = k
            image_tag = v["new_value"]

            full_docker_image_name = GetDockerImageFromDiff(k, image_tag)
            docker_image_to_rebuild = FindImagePart(k)
            docker_image_tuple = (full_docker_image_name, docker_image_to_rebuild, image_tag)
            docker_image_tuples.append(docker_image_tuple)

        # Reshape the data
        docker_image_tuples = list(set(docker_image_tuples))
        found_images = []
        # Concert tuple to dict
        for tuple in docker_image_tuples:
            image = {}
            image["full-image"] = tuple[0]
            image["short-name"] = tuple[1]
            image["image-tag"] = tuple[2]
            found_images.append(image)

        logging.info("\tCross reference docker images with lookup")
        short_name_to_github_repo = {}
        images_short_names_of_interest = []
        for mapping in config["github-repo-image-lookup"]:
            short_name_to_github_repo[mapping["image"]] = mapping["github-repo"]
            images_short_names_of_interest.append(mapping["image"])

        for found_image in found_images:
            if found_image["short-name"] in images_short_names_of_interest:
                logging.info("\tFound image {}".format(found_image))

                # Create the Github repo, if not present
                github_repo = short_name_to_github_repo[found_image["short-name"]]
                if github_repo not in images_to_rebuild:
                    images_to_rebuild[github_repo] = []

                # Check to see if this is a new image
                if found_image["full-image"] not in list(map(lambda e: e["full-image"], images_to_rebuild[github_repo])):
                    # This is a new image
                    found_image["csm-releases"] = [branch]
                    images_to_rebuild[github_repo].append(found_image)
                else:
                    # Add the accompanying CSM release branch to an image that was already found in a different CSM release
                    for image in images_to_rebuild[github_repo]:
                        if found_image["full-image"] == image["full-image"]:
                            image["csm-releases"].append(branch)

    ####################
    # Start to process helm charts
    ####################
    charts_to_download = []
    helm_lookup = config["helm-repo-lookup"]
    logging.info("find helm charts")

    all_charts = {}
    for branch in config["configuration"]["targeted-csm-branches"]:
        logging.info("Checking out CSM branch {} for helm chart image extraction".format(branch))
        csm_repo.git.checkout(branch)
        
        # its possible the same helm chart is referenced multiple times, so we should collapse the list
        # example download link: https://artifactory.algol60.net/artifactory/csm-helm-charts/stable/cray-hms-bss/cray-hms-bss-2.0.4.tgz
        # Ive added the helm-lookup struct because its a bunch of 'black magic' how the CSM repo knows where to download charts from
        # the hms-hmcollector is the exception that broke the rule, so a lookup is needed.

        helm_files = glob.glob(os.path.join(csm_dir, config["configuration"]["helm-manifest-directory"]) + "/*.yaml")
        for helm_file in helm_files:
            logging.info("Processing manifest {}".format(helm_file))
            with open(helm_file) as stream:
                try:
                    manifest = yaml.safe_load(stream)
                except yaml.YAMLError as exc:
                    logging.error("Failed to parse manifest {}, error: {}".format(helm_file, exc))
                    # If there is malformed manifest in the CSM manifest, then this entire workflow will fail.
                    # TODO Instead we should make a best effort attempt at rebuilding images, but we should exist an non-zero exit code
                    # to signal that not all images were rebuilt.
                    continue
            upstream_sources = {}
            for chart in manifest["spec"]["sources"]["charts"]:
                upstream_sources[chart["name"]] = chart["location"]
            for chart in manifest["spec"]["charts"]:
                chart_name = chart["name"]
                chart_version = chart["version"]
                if re.search(config["configuration"]["target-chart-regex"], chart["name"]) is not None:
                    # TODO this is happy path only, im ignoring any mis-lookups; need to fix it!
                    # TODO We are also ignore unlikely situations where different CSM releases pull the same helm chart version from different locations.
                    download_url = None
                    for repo in helm_lookup:
                        if repo["chart"] == chart["name"]:
                            download_url = urljoin(upstream_sources[chart["source"]],
                                                              os.path.join(repo["path"], chart_name + "-" + str(
                                                                  chart_version) + ".tgz"))

                    # Save chart overrides
                    # ASSUMPTION: It is being assumed that a HMS helm chart will be referenced only once in all loftsman manifests for any
                    # CSM release. The following logic will need to change, if we every decide to deploy the same helm chart multiple times
                    # with different release names.                   
                    if chart_name not in all_charts:
                        all_charts[chart_name] = {}
                    if chart_version not in all_charts[chart_name]:
                        all_charts[chart_name][chart_version] = {}
                        all_charts[chart_name][chart_version]["csm-releases"] = {} 
                        all_charts[chart_name][chart_version]["download-url"] = download_url
    
                    all_charts[chart_name][chart_version]["csm-releases"][branch] = {}
                    if "values" in chart:
                        all_charts[chart_name][chart_version]["csm-releases"][branch]["values"] = chart["values"]

    # The following is really ugly, but prints out a nice summary of the chart overrides across all of the CSM branches this script it is looking at.
    # This looks ugly, as I'm preferring to make the helm templating process later in this script nicer.
    logging.info("Manifest value overrides")
    manifest_values_overrides = {}
    for branch in config["configuration"]["targeted-csm-branches"]:
        manifest_values_overrides[branch] = {}

        for chart_name, versions in all_charts.items():
            for version_information in versions.values():
                if branch in version_information["csm-releases"] and "values" in version_information["csm-releases"][branch]:
                    manifest_values_overrides[branch][chart_name] = version_information["csm-releases"][branch]["values"]
    logging.info("\n"+yaml.dump(manifest_values_overrides))

    ######
    # Go download helm charts and explore them
    ######

    helm_dir = "helm_charts"
    # Clean up in case it exsts
    if os.path.exists(helm_dir):
        shutil.rmtree(helm_dir)

    os.mkdir(helm_dir)
    logging.info("download helm charts")
    
    # Extract all of the download links from the charts.
    charts_to_download = []
    for chart in all_charts.values():
        charts_to_download.extend(list(map(lambda e: chart[e]["download-url"], chart)))

    for chart in charts_to_download:
        r = requests.get(chart, stream=True)
        chart_url = []
        chart_url = chart.split('/')
        file_name = chart_url[-1]
        download_file_path = os.path.join(helm_dir, file_name)
        # download started
        with open(download_file_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        # TODO need to check if the file downloaded or not

        folder_name = file_name.replace('.tgz', '')
        file = tarfile.open(download_file_path)
        file.extractall(os.path.join(helm_dir, folder_name))
        file.close()

    logging.info("process helm charts")
    # the structure is well known: {helm-chart}-version/{helm-chart}/all-the-goodness-we-want
    for file in os.listdir(helm_dir):
        helm_chart_dir = os.path.join(helm_dir, file)
        if os.path.isdir(helm_chart_dir):
            for entry in os.listdir(helm_chart_dir):
                chart_dir = os.path.join(helm_chart_dir, entry)
                if os.path.isdir(chart_dir):
                    logging.info("Processing chart: {}".format(chart_dir))
                    with open(os.path.join(chart_dir, "Chart.yaml")) as stream:
                        try:
                            chart = yaml.safe_load(stream)
                        except yaml.YAMLError as exc:
                            logging.error(exc)
                            exit(1)
                    with open(os.path.join(chart_dir, "values.yaml")) as stream:
                        try:
                            values = yaml.safe_load(stream)
                        except yaml.YAMLError as exc:
                            logging.error(exc)
                            exit(1)
                    # Do Some stuff with this chart info
                    # THIS ASSUMES there is only one source and its the 0th one that we care about. I believe this is true for HMS
                    source = chart["sources"][0]
                    github_repo = source.split('/')[-1]
                    logging.info("\tGithub repo: {}".format(github_repo))

                    if github_repo not in images_to_rebuild:
                        images_to_rebuild[github_repo] = []

                    ## Assumed values.yaml structure
                    # global:
                    #  appVersion: 2.1.0
                    #  testVersion: 2.1.0
                    # tests:
                    #  image:
                    #    repository: artifactory.algol60.net/csm-docker/stable/cray-capmc-test
                    #    pullPolicy: IfNotPresent
                    #
                    # image:
                    #  repository: artifactory.algol60.net/csm-docker/stable/cray-capmc
                    #  pullPolicy: IfNotPresent
                    ### Its possible that there might not be a 'tests' value, but I will handle that.

                    # Determine the names of the main application image, and the test image
                    images_repos_of_interest = []
                    images_repos_of_interest.append(values["image"]["repository"])
                    if "testVersion" in values["global"]:
                        images_repos_of_interest.append(values["tests"]["image"]["repository"])

                    logging.info("\tImage repos of interest:")
                    for image_repo in images_repos_of_interest:
                        logging.info("\t- {}".format(image_repo))

                    # Now template the Helm chart to learn the image tags
                    for branch in all_charts[chart["name"]][chart["version"]]["csm-releases"]:
                        logging.info("\tCSM Branch {}".format(branch))
                        chart_value_overrides = all_charts[chart["name"]][chart["version"]]["csm-releases"][branch].get("values")
                        
                        # Write out value overrides
                        values_override_path = os.path.join(helm_chart_dir, "values-{}.yaml".format(branch.replace("/", "-")))
                        logging.info("\t\tWriting out value overrides {}".format(values_override_path))
                        with open(values_override_path, "w") as f:
                            yaml.dump(chart_value_overrides, f)

                        # TODO thought about inlining this script, but using shell=True can be dangerous.
                        result = subprocess.run(["./extract_chart_images.sh", chart_dir, values_override_path], capture_output=True, text=True)
                        if result.returncode != 0:
                            logging.error("Failed to extract images from chart. Exit code {}".format(result.returncode))
                            logging.error("stderr: {}".format(result.stderr))
                            logging.error("stdout: {}".format(result.stdout))
                            exit(1)

                        logging.info("\t\tImages in use:")
                        for image in result.stdout.splitlines():
                            image_repo, image_tag = image.split(":", 2)

                            if image_repo not in images_repos_of_interest:
                                continue
                            logging.info("\t\t- {}".format(image))

                            # Add the image to the list to be rebuilt if this is a new image
                            if image not in list(map(lambda e: e["full-image"], images_to_rebuild[github_repo])):
                                images_to_rebuild[github_repo].append({
                                    "full-image": image,
                                    "short-name": image_repo.split('/')[-1],
                                    "image-tag": image_tag,
                                    "csm-releases": [branch]
                                })
                            else:
                                # Add the accompanying CSM release branch to an image that was already found in a different CSM release
                                for image in images_to_rebuild[github_repo]:
                                    if found_image["full-image"] == image["full-image"]:
                                        image["csm-releases"].append(branch)


    #################
    # Launch Rebuilds
    #################
    logging.info("attempting to identify workflows")

    desired_workflow_names = ["Build and Publish Service Docker Images", "Build and Publish Docker Images",
                              "Build and Publish CT Docker Images"]
    # Todo what about the hms-test repo workflow? Build and Publish hms-test ... for now we will ignore it.

    for repo_name, val in images_to_rebuild.items():
        images = images_to_rebuild[repo_name]  # im going to be writing back to this
        repo_data = g.get_organization("Cray-HPE").get_repo(repo_name)
        available_workflows = []
        for workflow in repo_data.get_workflows():
            if workflow.name in desired_workflow_names:
                available_workflows.append(workflow)

        for image in images:
            image_tag = image["image-tag"]
            git_tag = "v" + str(image_tag)  # we always tag like v1.2.3
            short_name = image["short-name"]
            full_image = image["full-image"]
            commit = None

            is_test = re.search(".*-test", short_name)

            tags = []
            for tag in repo_data.get_tags():
                tags.append(tag)
            for tag in tags:
                if tag.name == git_tag:
                    commit = tag.commit.commit.sha
                    image["commit"] = commit
                    break  # no reason to continue

            # todo need to have error checking in case we cant match the tag! commit will be None
            # todo need to identify if a image never gets built! or has no workflows, or isnt happy path!
            # todo error checking for launched
            launched = False
            for available_workflow in available_workflows:
                wf = {}
                if is_test is not None and available_workflow.name == "Build and Publish CT Docker Images":  # this is a test image and the CT image workflow
                    # launched = available_workflow.create_dispatch(git_tag)
                    wf = available_workflow
                    image["workflow"] = wf
                    
                elif is_test is None and available_workflow.name != "Build and Publish CT Docker Images":  # this is NOT a test, and we are NOT using the CT image workflow
                    # launched = available_workflow.create_dispatch(git_tag)
                    wf = available_workflow
                    image["workflow"] = wf


            if "workflow" not in image:
                logging.warn("Unable to determine workflow for image {} in Github repository {}".format(image["full-image"], repo_name))

            image["git-tag"] = git_tag
            # image["workflow-initiated"] = launched

    summary = {}
    if not dry_run:
        # This is ugly, but Github is stupid and refuses to return an ID for a create-dispatch
        # https://stackoverflow.com/questions/69479400/get-run-id-after-triggering-a-github-workflow-dispatch-event
        # https://github.com/github-community/community/discussions/9752
        logging.info("attempting to launch workflows")

        # Go get the runs
        for k, v in images_to_rebuild.items():
            images = images_to_rebuild[k]
            for image in images:
                workflow = image["workflow"]
                image["pre-runs"] = []
                for run in workflow.get_runs(event="workflow_dispatch", branch=image["git-tag"]):
                    image["pre-runs"].append(run)
                image["workflow-initiated"] = image["workflow"].create_dispatch(image["git-tag"])

        # wait X seconds since github actions are launched on a web-hook; this might not be enough time
        time.sleep(webhook_sleep_seconds)

        logging.info("attempting to find launched workflows")

        # Go get the runs
        for k, v in images_to_rebuild.items():
            images = images_to_rebuild[k]
            for image in images:
                workflow = image["workflow"]
                image["post-runs"] = []
                if image["workflow-initiated"]:
                    for run in workflow.get_runs(event="workflow_dispatch", branch=image["git-tag"]):
                        image["post-runs"].append(run)

        # now collate
        targeted_workflows = []
        for k, v in images_to_rebuild.items():
            images = images_to_rebuild[k]
            for image in images:
                pre = []
                post = []
                image["targeted-workflows"] = []

                for p in image["pre-runs"]:
                    pre.append(p.id)
                for p in image["post-runs"]:
                    post.append(p.id)

                diff = list(set(post) - set(pre))
                image["targeted-runs"] = diff

                for p in image["post-runs"]:
                    if p.id in diff:
                        image["targeted-workflows"].append(p)
                        targeted_workflows.append(p)

        # Im tired of traversing the whole dictionary - and then asking about all workflows...
        # Im going to use the targeted_workflows list and just use the API, because there is a lacking PyGithub interface for what I need.
        # I dont see it in the list: https://pygithub.readthedocs.io/en/latest/github_objects.html

        complete = False
        expiration_time = datetime.now() + timedelta(minutes=expiration_minutes)
        last_request = {}


        while not complete or expiration_time < datetime.now():

            complete = True
            status = []
            for t in targeted_workflows:

                query_url = t.url
                headers = {'Authorization': f'token {github_token}'}
                r = requests.get(query_url, headers=headers)
                data = json.loads(r.text)
                last_request[t.id] = data
                if r.status_code == 200:
                    status.append(data["status"])

            occurrences = collections.Counter(status)
            if occurrences["completed"] != len(targeted_workflows):
                complete = False
            logging.info("waiting for completion\t" + str(occurrences) + "\t sleeping " + str(sleep_duration) + " seconds")
            time.sleep(sleep_duration)

        conclusion = []
        temp = []
        for key, val in last_request.items():
            conclusion.append(val["conclusion"])

        conclusion_disposition = collections.Counter(conclusion)
        summary["summary"] = conclusion_disposition

    rebuilt_images = copy.deepcopy(images_to_rebuild)
    # make a copy of the dictionary and clean it up, so I can JSON dump it.
    for k, v in rebuilt_images.items():
        images = rebuilt_images[k]
        for image in images:

            image.pop("pre-runs", None)
            image.pop("post-runs", None)
            image.pop("monitor-runs", None)
            image.pop("diff", None)
            image.pop("workflow-initiated", None)
            image.pop("targeted-workflows", None)

            # The data below is not generated for dry runs
            if dry_run:
                image.pop("workflow", None)
                continue

            image["executions"] = []
            for tr in image["targeted-runs"]:
                if tr in last_request:
                    datum = last_request[tr]
                    data = {}
                    data["job-id"] = datum["id"]
                    data["job-status"] = datum["status"]
                    data["job-conclusion"] = datum["conclusion"]
                    data["job-url"] = datum["url"]
                    data["job-html-url"] = datum["html_url"]
                    data["workflow-url"] = image["workflow"].url
                    data["workflow-name"] = image["workflow"].name

                    image["executions"].append(data)

            image.pop("workflow", None)
            image.pop("targeted-runs", None)


    logging.info(json.dumps(rebuilt_images, indent=2))
    logging.info(summary)

    # Generate output file for job status templating, if /output exists
    if os.path.exists("/output"):
        logging.info("Generating job summary template values")
        template_values = CreateJobSummaryTemplateValues(rebuilt_images, summary)
        with open("/output/job_summary_template_values.yaml", "w") as f:
            yaml.dump(template_values, f)

    if "summary" in summary and summary["summary"]["success"] != len(targeted_workflows):
        logging.error("some workflows did not report success")
        exit(1)
    logging.info("all workflows successfully completed")
    exit(0)

