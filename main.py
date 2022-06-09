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


def GetTargetedDockerImagesFromManifest(path, imageKeys):
    return "fp"


if __name__ == '__main__':

    ####################
    # Load Configuration
    ####################

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

    csm = config["CSM-manifest-repo-name"]
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
    docker_image_tuples = []
    for branch in config["targeted-csm-branches"]:
        csm_repo.git.checkout(branch)
        # print("Setting branch to:" + csm_repo.active_branch.name)

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
    for tuple in docker_image_tuples:
        data = {}
        data["images"] = []
        image_name = tuple[1]
        if image_name in images_to_rebuild:
            data = images_to_rebuild[image_name]
        datum = {}
        datum["full-image"] = tuple[0]
        datum["image-tag"] = tuple[2]
        data["images"].append(datum)
        images_to_rebuild[image_name] = data

    # add in the repo information
    with open('repo-image-lookup.json', 'r') as file:
        repo_lookup = json.load(file)
    #
    for item in repo_lookup:
        if item["image"] in images_to_rebuild:
            images_to_rebuild[item["image"]]["repo"] = item["repo"]

    ####################
    # Start to process helm charts
    ####################
    charts_to_download = []
    with open('helm-lookup.json', 'r') as file:
        helm_lookup = json.load(file)

    for branch in config["targeted-csm-branches"]:
        csm_repo.git.checkout(branch)

        # its possible the same helm chart is referenced multiple times, so we should collapse the list
        # TODO its al so possible that a docker-image override is specified, we HAVE TO check for that!
        # example download link: https://artifactory.algol60.net/artifactory/csm-helm-charts/stable/cray-hms-bss/cray-hms-bss-2.0.4.tgz
        # TODO will helm charts always be in stable?

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
                #TODO this is happy path only, im ignoring any mis-lookups; need to fix it!
                    for item in helm_lookup:
                        if item["chart"] == chart["name"]:
                            charts_to_download.append(urljoin(upstream_sources[chart["source"]],
                                                              os.path.join(item["path"], chart["name"] + "-" + str(
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

    for chart in charts_to_download:
        r = requests.get(chart, stream=True)
        chart_url = []
        chart_url = chart.split('/')
        file_name = chart_url[-1]
        download_file_path = os.path.join(helm_dir,file_name)
        # download started
        with open(download_file_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        #TODO need to check if the file downloaded or not


        folder_name = file_name.replace('.tgz','')
        file = tarfile.open(download_file_path)
        file.extractall(os.path.join(helm_dir,folder_name))
        file.close()
#
# print(csm_repo_metadata.get_branch("main").name)
