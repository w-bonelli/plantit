# <i class="fas fa-stream fa-1x fa-fw"></i> **Workflows**

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Binding workflows: the `plantit.yaml` file](#binding-workflows-the-plantityaml-file)
  - [Contact email](#contact-email)
  - [Jobqueue requests](#jobqueue-requests)
    - [Walltime](#walltime)
    - [Virtual memory](#virtual-memory)
  - [GPU mode](#gpu-mode)
  - [Environment variables](#environment-variables)
  - [Parameters](#parameters)
    - [Default values](#default-values)
  - [Shell selection](#shell-selection)
  - [Input/output](#inputoutput)
    - [Inputs](#inputs)
      - [Input types (`file`, `files`, and `directory`)](#input-types-file-files-and-directory)
      - [Input filetypes](#input-filetypes)
    - [Outputs](#outputs)
- [A simple example](#a-simple-example)
- [Using workflows: the `plantit` web UI](#using-workflows-the-plantit-web-ui)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

## Binding workflows: the `plantit.yaml` file

To host software (a <i class="fas fa-stream fa-1x fa-fw"></i> **Workflow**) on PlantIT, add a `plantit.yaml` file to your GitHub repository. At minimum, it should look something like this:

```yaml
name: Hello Groot             
author: Groot                
public: True                  # visible to other users of PlantIT?
image: docker://alpine        # Docker image to use
commands: echo "I am Groot!"  # your entry point
```

There are a number of optional attributes as well.

### Contact email

You can provide a contact email via an `email` attribute. If this attribute is provided, a `mailto` link will be shown in the user interface to allow your workflow's users to easily contact you.

### Jobqueue requests

To make sure your workflow can take full advantage of cluster resources, add a `jobqueue` section to your `plantit.yaml` file. For example, to indicate that an instance of your workflow should request 1 process and 1 core on 1 node with 1 GB of memory for 1 hour:

```yaml
jobqueue:
  walltime: "01:00:00"
  memory: "1GB"
  processes: 1
  cores: 1
```

If you do not provide a `jobqueue` section in your `plantit.yaml`, tasks deployed to any agent will request 1 hour of walltime, 10 GB of RAM, 1 process, and 1 core.

#### Walltime

When a `plantit` task is submitted, values provided for the `jobqueue.walltime` attribute will be passed through transparently to the selected deployment target's scheduler. The `plantit` web UI will timestamp each task submission,  If such a time limit is provided at submission time, `plantit` will attempt to cancel your task if it fails to complete before the time limit has elapsed.

#### Virtual memory

Note that some deployment targets (namely the default public agent, [TACC's Stampede2](https://www.tacc.utexas.edu/systems/stampede2)) are equipped with virtual memory. For tasks deployed to agents with virtual memory, `plantit` will ignore values provided for the `jobqueue.memory` attribute and defer to the cluster scheduler: on Stampede2, for instance, all tasks have access to 98GB of RAM.

### GPU mode

To indicate that your workflow can take advantage of GPUs (only available on select deployment targets) add a `gpu: True` line to your configuration file. When deployed to environments with GPUs, your task will have access to an environment variable `GPUS`, set to the number of GPU devices provided by the host.

### Environment variables

Certain environment variables will be automatically set in the Singularity container runtime when a workflow is submitted as a task, in case you need to reference them in your entry point command or script:

- WORKDIR: the current working directory
- INPUT: the path to an input file or directory
- OUTPUT: the path to the directory results will be written to
- INDEX: the index of the current input file (if there are multiple, otherwise defaults to 1 for single-file or -directory tasks)

You can provide custom environment variables in an `env` section, for instance:

```yaml
...
env:
  - LC_ALL=C.UTF-8
  - LANG=C.UTF-8
...
```

### Parameters

To parametrize your workflow, add a `params` section. For example, to allow the user to configure the message printed by the trivial workflow above:

```yaml
name: Hello Who?
author: Groot
public: True
clone: False
image: docker://alpine
commands: echo "$MESSAGE"
params:
 - name: message
   type: string
```

This will cause the value of `message`, specified in the `plantit` web UI at task submission time, to be substituted for `$MESSAGE` in the `command` at runtime.

Four parameter types are supported by `plantit`:

- `string`
- `select`
- `number`
- `boolean`

See the `Computational-Plant-Science/plantit-example-parameters` workflow [on GitHub](https://github.com/Computational-Plant-Science/plantit-example-parameters/blob/master/plantit.yaml) for an example of how to use parameters.

#### Default values

To provide default values for your workflow's parameters, you can use a `default` attribute. For instance:

```yaml
params:
 - name: message
   type: string
   default: 'Hello, world!'
```

### Shell selection

By default, the `command` specified in `plantit.yaml` is invoked directly from the Singularity container runtime, i.e., `singularity exec <image> <command>`. Since [Singularity runs in a modified shell environment](https://stackoverflow.com/a/56490063) some behavior may differ from that produced by Docker. Some Anaconda-based images can be configured to automatically activate an environment, for instance. With Singularity this cannot be achieved without wrapping the `command` with `bash -c '<command>'` and [editing bash startup files in the container definition](https://stackoverflow.com/a/57441264).

For these reasons `plantit` provides a `shell` option. If provided, this option will cause `plantit` to wrap the `command` with `... <shell> -c 'command'` when invoking Singularity. Supported values include:

- `bash`
- `sh`
- `zsh`

### Input/output

`plantit` can automatically copy input files from the [CyVerse Data Store](https://www.cyverse.org/data-store) or [Data Commons](https://cyverse.org/data-commons) onto the file system in your deployment environment, then push results back to the Data Store after your task completes. To configure inputs and outputs for a workflow, add `input` and `output` attributes to your configuration.

#### Inputs

If your workflow requires inputs, add an `input` section to your configuration file, containing at minimum a `path` attribute (pointing either to a directory- or file-path in the CyVerse Data Store or Data Commons, or left blank) and a `kind` attribute indicating whether this workflow operates on a single `file`, multiple `files`, or an entire `directory`.

##### Input types (`file`, `files`, and `directory`)

To indicate that your workflow should pull a single file from the Data Store and spawn a single container to process it, use `kind: file`. To pull a directory from the Data Store and spawn a container for each file, use `kind: files`. To pull a directory and spawn a single container to process it, use `kind: directory`.

It's generally a good idea for `path` to reference a community-released or curated public dataset in the CyVerse Data Commons, so prospective users can test your workflow on real data. For instance, the `plantit.yaml` for a workflow which operates on a single file might have the following `input` section

```yaml
input:
  path: /iplant/home/shared/iplantcollaborative/testing_tools/cowsay/cowsay.txt
  kind: file
```

##### Input filetypes

To specify which filetypes your workflow is permitted to accept, add a `filetypes` attribute to the `input` section:

```yaml
input:
  path: /iplant/home/shared/iplantcollaborative/testing_tools/cowsay
  kind: file
  filetypes:
    - txt
```

Any values provided to `filetypes` will be joined (with `,`) and substituted for `FILETYPES` in your workflow's command. Use this to inform your code which filetypes to expect, for example:

```yaml
commands: ls "$INPUT"/*.{"FILETYPES"} >> things_cow_say.txt
input:
  path: /iplant/home/shared/iplantcollaborative/testing_tools/cowsay
  kind: file
  filetypes:
    - txt
    - md     
```

*Note that while the `input.path` and `input.filetypes` attributes are optional, you must provide a `kind` attribute if you provide an `input` section.*

#### Outputs

If your workflow produces outputs, add an `output` section with a `path` attribute to your configuration file. This attribute may be left blank if your workflow writes output files to the working directory; otherwise the value should be a directory path relative to the working directory. For example, to indicate that your workflow will deposit output files in a directory `output/directory` relative to the workflow's working directory:

```yaml
output:
  path: output/directory
```

By default, all files under the given `path` are uploaded to the location in the CyVerse Data Store provided by the user. To explicitly indicate which files to include/exclude (this is suggested especially if you workflow deposits files in the working directory), add `include` and `exclude` sections under `output`:

```yaml
output:
  path: output/directory
  include:
    patterns:                           # include excel files
      - xlsx                            # and png files
    names:                              
      - important.jpg                   # but only this .jpg file
  exclude:
    patterns:
      - temp                            # don't include anything marked temp
    names:
      - not_important.xlsx              # and exclude a particularexcel file
```

If only an `include` section is provided, only the file patterns and names specified will be included. If only an `exclude` section is present, all files except the patterns and names specified will be included. If you provide both `include` and `exclude` sections, the `include` rules will first be applied to generate a subset of files, which will then be filtered by the `exclude` rules.

## A simple example

The following workflow prints the content of an input file and then writes it to an output file located in the same working directory.

```yaml
name: Hello File
image: docker://alpine
public: True
commands: cat "$INPUT" && cat "$INPUT" >> cowsaid.txt
input:
  from: /iplant/home/shared/iplantcollaborative/testing_tools/cowsay/cowsay.txt
  kind: file
output:
  path: # blank to indicate working directory
  include:
    names:                              
      - cowsaid.txt
```

## Using workflows: the `plantit` web UI

To explore and run workflows deployed to `plantit`, navigate to the <i class="fas fa-stream fa-1x fa-fw"></i> **Workflows** tab from the home view.

![Workflows](../media/workflows.png)

By default, this page will display the <i class="fas fa-certificate fa-1x fa-fw"></i> **Featured** workflow context: a curated set of applications provided by the Computational Plant Science lab, collaborators, and other researchers. 

Click the <i class="fas fa-certificate fa-1x fa-fw"></i> **Featured** dropdown to select a different context. Options include:

- <i class="fas fa-pin fa-1x fa-fw"></i> **Examples**: a small set of simple workflows to serve as templates and examples
- <i class="fas fa-users fa-1x fa-fw"></i> **Public**: all publicly available workflows
- <i class="fas fa-user fa-1x fa-fw"></i> **Yours**: your own workflows (private and public)
- <i class="fas fa-building fa-1x fa-fw"></i> **[Organization]**: workflows belonging to a particular organization
- <i class="fas fa-sprout fa-1x fa-fw"></i> **[Project]**: workflows associated with a particular MIAPPE project

The `plantit` web application scrapes GitHub for repository information for all logged-in users every 5 minutes. (If you've just updated a repository, you may need to wait several minutes then reload the workflow page.)