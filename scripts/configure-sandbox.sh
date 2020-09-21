#!/bin/bash

echo "Configuring sandbox deployment target..."

script="
from plantit.targets.models import Target

name = 'Sandbox'
description = 'A cluster-in-a-container suitable for lightweight, in-process test deployments.'
workdir = '/test'
username = 'root'
password = 'root'
port = 22
hostname = 'sandbox'
pre_commands = 'export LC_ALL=C.UTF-8 \\\n export LANG=C.UTF-8'

if Target.objects.filter(name=name).count()==0:
    Target.objects.create(name=name, description=description, workdir=workdir, username=username, password=password, port=port, hostname=hostname, pre_commands=pre_commands)
    print('Configured sandbox.')
else:
    print('Sandbox already exists!')
"
printf "$script" | python manage.py shell