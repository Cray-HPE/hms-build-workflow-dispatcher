# MIT License

# (C) Copyright [2019-2022] Hewlett Packard Enterprise Development LP

# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

#FROM artifactory.algol60.net/csm-docker/stable/docker.io/library/alpine:3.16 as builder
FROM artifactory.algol60.net/docker.io/alpine:3.16 as builder

LABEL maintainer="Hewlett Packard Enterprise"
STOPSIGNAL SIGTERM

# Install the necessary packages.
RUN set -ex \
    && apk -U upgrade \
    && apk add --no-cache \
        python3 \
        py3-pip \
        bash \
        tar

COPY requirements.txt .

RUN pip3 install --upgrade pip

#FROM builder as installer


COPY workspace.py /usr/bin/workspace.py
COPY entrypoint.sh /src/app/entrypoint.sh
COPY docker-index-compare.yaml /src/app/docker-index-compare.yaml

#
## Run as nobody
#RUN chown  -R 65534:65534 /src
#USER 65534:65534

FROM builder as final
RUN pip3 install -r requirements.txt

WORKDIR /src/app
#ENTRYPOINT ["ls" ]
ENTRYPOINT [ "./entrypoint.sh" ]