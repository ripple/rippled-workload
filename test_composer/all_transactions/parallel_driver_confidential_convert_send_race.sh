#!/usr/bin/env bash
# Race: Convert (public→confidential) during pending Send — boundary race
curl --silent http://workload:8000/confidential/convert/random &
curl --silent http://workload:8000/confidential/send/random &
wait
