#!/usr/bin/env bash

docker rm -v $(docker ps --filter status=exited -q)
