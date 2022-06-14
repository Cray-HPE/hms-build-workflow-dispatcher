#!/usr/bin/env python3
import os
from github import Github
from git import Repo
import tempfile
import shutil
import yaml
from deepdiff import DeepDiff
import json
import glob
import re
from urllib.parse import urljoin
import requests
import tarfile
import time


# This is very procedural oriented code. I haven't split much of this into function calls, because I think the whole
# process will be ~500 lines of code.  TODO This could withstand some clean up, but encapsulation in methods in this case won't
# lend to reusability, just organization (which is still a valid reason).

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
    print("load configuration")
    with open('credentials.json', 'r') as file:
        data = json.load(file)
    github_username = data["github"]["username"]
    github_token = data["github"]["token"]

    with open('configuration.json', 'r') as file:
        config = json.load(file)

    g = Github(github_token)



    ####################
    # Download the CSM repo
    ####################
    print("retrieve manifest repo")

    csm = config["manifest-repo"]
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
    print("find docker images")

    docker_image_tuples = []
    for branch in config["targeted-csm-branches"]:
        csm_repo.git.checkout(branch)

        # load the docker index file
        docker_index = os.path.join(csm_dir, config["docker-image-manifest"])
        with open(docker_index) as stream:
            try:
                manifest = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)
                exit(1)

        docker_compare = os.path.join(config["docker-image-compare"])
        with open(docker_compare) as stream:
            try:
                compare = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)
                exit(1)

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

    # add in the repo information
    with open('repo-image-lookup.json', 'r') as file:
        repo_lookup = json.load(file)
    print("cross reference docker images with lookup")
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
    with open('helm-lookup.json', 'r') as file:
        helm_lookup = json.load(file)
    print("find helm charts")

    for branch in config["targeted-csm-branches"]:
        csm_repo.git.checkout(branch)

        # its possible the same helm chart is referenced multiple times, so we should collapse the list
        # TODO its al so possible that a docker-image override is specified, we HAVE TO check for that!
        # example download link: https://artifactory.algol60.net/artifactory/csm-helm-charts/stable/cray-hms-bss/cray-hms-bss-2.0.4.tgz
        # TODO will helm charts always be in stable?
        # Ive added the helm-lookup file because its a bunch of 'black magic' how the CSM repo knows where to download charts from
        # the hms-hmcollector is the exception that broke the rule, so a lookup is needed.

        helm_files = glob.glob(os.path.join(csm_dir, config["helm-manifest-directory"]) + "/*.yaml")
        for helm_file in helm_files:
            with open(helm_file) as stream:
                try:
                    manifest = yaml.safe_load(stream)
                except yaml.YAMLError as exc:
                    print(exc)
                    exit(1)
            upstream_sources = {}
            for chart in manifest["spec"]["sources"]["charts"]:
                upstream_sources[chart["name"]] = chart["location"]
            for chart in manifest["spec"]["charts"]:
                if re.search(config["target-chart-regex"], chart["name"]) is not None:
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
    print("download helm charts")

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

    print("process helm charts")
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
                            print(exc)
                            exit(1)  # todo need to do something else
                    with open(os.path.join(chart_dir, "values.yaml")) as stream:
                        try:
                            values = yaml.safe_load(stream)
                        except yaml.YAMLError as exc:
                            print(exc)
                            exit(1)  # todo need to do something else
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
    print("attempting to launch workflows")

    desired_workflow_names = ["Build and Publish Service Docker Images", "Build and Publish Docker Images",
                              "Build and Publish CT Docker Images"] #Todo what about the hms-test repo workflow? Build and Publish hms-test ...


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
                    #launched = available_workflow.create_dispatch(git_tag)
                    wf = available_workflow
                    image["workflow"] = wf
                elif is_test is None and available_workflow.name != "Build and Publish CT Docker Images":  # this is NOT a test, and we are NOT using the CT image workflow
                    #launched = available_workflow.create_dispatch(git_tag)
                    wf = available_workflow
                    image["workflow"] = wf
            image["git-tag"] = git_tag
            #image["workflow-initiated"] = launched


    print(images_to_rebuild)

    #todo I need to somehow figure out what the ID is for each run and keep checking up on it.
    # This is ugly, but Github is stupid and refuses to return an ID for a create-dispatch
    # https://stackoverflow.com/questions/69479400/get-run-id-after-triggering-a-github-workflow-dispatch-event
    # https://github.com/github-community/community/discussions/9752

    #Go get the runs
    for k,v in images_to_rebuild.items():
        images = images_to_rebuild[k]
        for image in images:
            workflow = image["workflow"]
            image["pre-runs"] = []
            for run in workflow.get_runs(event="workflow_dispatch", branch=image["git-tag"]):
                image["pre-runs"].append(run)
            image["workflow-initiated"] = image["workflow"].create_dispatch(image["git-tag"])


    time.sleep(5)
    #wait 5 seconds since github actions are launched on a web-hook; this might not be enough time
    # Go get the runs
    for k, v in images_to_rebuild.items():
        images = images_to_rebuild[k]
        for image in images:
            workflow = image["workflow"]
            image["post-runs"] = []
            if image["workflow-initiated"] :
                for run in workflow.get_runs(event="workflow_dispatch", branch=image["git-tag"]):
                    image["post-runs"].append(run)


    #now collate
    for k, v in images_to_rebuild.items():
        images = images_to_rebuild[k]
        for image in images:
            pre = image["pre-runs"]
            post = image["post-runs"]

            diff = list(set(post) - set(pre))
            image["diff"] = diff

            image.pop("pre-runs", None)
            image.pop("post-runs", None)

            # found = False
            # for po in post:
            #     for pr in pre:
            #         if po.id == pr.id:
            #             image["workflow-run-id"] = po.id

    print(images_to_rebuild)
