NAME ?= hms-build-workflow-dispatcher
VERSION ?= $(shell cat .version)

all: image

#todo fix this target thing!

image:
	docker build --pull ${DOCKER_ARGS} --tag '${NAME}:${VERSION}' .

