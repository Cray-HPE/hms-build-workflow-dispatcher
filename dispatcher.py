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


if __name__ == '__main__':

    ####################
    # Load Configuration
    ####################

    # with open('credentials.json', 'r') as file:
    #     data = json.load(file)
    # github_username = data["github"]["username"]
    # github_token = data["github"]["token"]

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

    docker_image_tuples = []
    for branch in config["configuration"]["targeted-csm-branches"]:
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
    for k, v in changed.items():
        path_to_digest = k
        image_tag = v["new_value"]

        full_docker_image_name = GetDockerImageFromDiff(k, image_tag)
        docker_image_to_rebuild = FindImagePart(k)
        docker_image_tuple = (full_docker_image_name, docker_image_to_rebuild, image_tag)
        docker_image_tuples.append(docker_image_tuple)

    # Reshape the data
    docker_image_tuples = list(set(docker_image_tuples))
    images_to_rebuild = {}
    images = []
    # Concert tuple to dict
    for tuple in docker_image_tuples:
        # data = {}
        # data["images"] = []
        # image_name = tuple[1]
        # if image_name in images_to_rebuild:
        #     data = images_to_rebuild[image_name]
        image = {}
        image["full-image"] = tuple[0]
        image["short-name"] = tuple[1]
        image["image-tag"] = tuple[2]
        images.append(image)
        # data["images"].append(datum)
        # images_to_rebuild[image_name] = data

    repo_lookup = config["repo-image-lookup"]
    logging.info("cross reference docker images with lookup")
    for repo in repo_lookup:
        for image in images:
            if repo["image"] == image["short-name"]:
                if repo["repo"] in images_to_rebuild:
                    repo_val = images_to_rebuild[repo["repo"]]
                    repo_val.append(image)
                    repo["repo"] = repo_val
                else:
                    images_to_rebuild[repo["repo"]] = []
                    images_to_rebuild[repo["repo"]].append(image)

    ####################
    # Start to process helm charts
    ####################
    charts_to_download = []
    helm_lookup = config["helm-repo-lookup"]
    logging.info("find helm charts")

    for branch in config["configuration"]["targeted-csm-branches"]:
        csm_repo.git.checkout(branch)

        # its possible the same helm chart is referenced multiple times, so we should collapse the list
        # TODO its al so possible that a docker-image override is specified, we HAVE TO check for that!
            # values
            #    global:
            #        appVersion: 2.1.0
        # example download link: https://artifactory.algol60.net/artifactory/csm-helm-charts/stable/cray-hms-bss/cray-hms-bss-2.0.4.tgz
        # Ive added the helm-lookup struct because its a bunch of 'black magic' how the CSM repo knows where to download charts from
        # the hms-hmcollector is the exception that broke the rule, so a lookup is needed.

        helm_files = glob.glob(os.path.join(csm_dir, config["configuration"]["helm-manifest-directory"]) + "/*.yaml")
        for helm_file in helm_files:
            with open(helm_file) as stream:
                try:
                    manifest = yaml.safe_load(stream)
                except yaml.YAMLError as exc:
                    logging.error(exc)
                    exit(1)
            upstream_sources = {}
            for chart in manifest["spec"]["sources"]["charts"]:
                upstream_sources[chart["name"]] = chart["location"]
            for chart in manifest["spec"]["charts"]:
                if re.search(config["configuration"]["target-chart-regex"], chart["name"]) is not None:
                    # TODO this is happy path only, im ignoring any mis-lookups; need to fix it!
                    for repo in helm_lookup:
                        if repo["chart"] == chart["name"]:
                            charts_to_download.append(urljoin(upstream_sources[chart["source"]],
                                                              os.path.join(repo["path"], chart["name"] + "-" + str(
                                                                  chart["version"]) + ".tgz")))
    charts_to_download = sorted(list(set(charts_to_download)))

    ######
    # Go download helm charts and explore them
    ######

    helm_dir = "helm_charts"
    # Clean up in case it exsts
    if os.path.exists(helm_dir):
        shutil.rmtree(helm_dir)

    os.mkdir(helm_dir)
    logging.info("download helm charts")

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
                    repo = source.split('/')[-1]

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

                    main_image_tag = values["global"]["appVersion"]
                    main_image = values["image"]["repository"]
                    main_short_image = main_image.split('/')[-1]
                    test_image_tag = None
                    test_image = None
                    test_short_image = None
                    if "testVersion" in values["global"]:
                        test_image_tag = values["global"]["testVersion"]
                        test_image = values["tests"]["image"]["repository"]
                        test_short_image = test_image.split('/')[-1]

                    images = []
                    image = {}
                    image["full-image"] = main_image
                    image["short-name"] = main_short_image
                    image["image-tag"] = main_image_tag
                    images.append(image)
                    if test_image is not None:
                        image = {}
                        image["full-image"] = test_image
                        image["short-name"] = test_short_image
                        image["image-tag"] = test_image_tag
                        images.append(image)

                    if repo in images_to_rebuild:
                        repo_val = images_to_rebuild[repo]
                        repo_val.extend(images)
                        images_to_rebuild[repo] = repo_val
                    else:
                        images_to_rebuild[repo] = []
                        images_to_rebuild[repo].extend(images)

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
            image["git-tag"] = git_tag
            # image["workflow-initiated"] = launched

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
    summary = {}
    summary["summary"] = conclusion_disposition

    rebuilt_images = copy.deepcopy(images_to_rebuild)
    # make a copy of the dictionary and clean it up, so I can JSON dump it.
    for k, v in rebuilt_images.items():
        images = rebuilt_images[k]
        for image in images:

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

            image.pop("pre-runs", None)
            image.pop("post-runs", None)
            image.pop("monitor-runs", None)
            image.pop("diff", None)
            image.pop("workflow-initiated", None)
            image.pop("targeted-runs", None)
            image.pop("targeted-workflows", None)
            image.pop("workflow", None)

    logging.info(json.dumps(rebuilt_images, indent=2))
    logging.info(summary)

    if conclusion_disposition["success"] != len(targeted_workflows):
        logging.error("some workflows did not report success")
        exit(1)
    logging.info("all workflows successfully completed")
    exit(0)
