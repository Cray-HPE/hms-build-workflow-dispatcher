<!-- This file is templated with https://pkg.go.dev/html/template -->

# HMS Build workflow dispatcher

| Github Repository | Git Tag | Docker image | Present in CSM Releases | Workflow Status | Launched workflow | 
{{ range $image := .images -}}
| {{ $image.git_repo }} | [{{ $image.git_tag }}](https://github.com/Cray-HPE/{{ $image.git_repo }}/releases/tag/{{ $image.git_tag }}) | {{ $image.full_image }} | __TODO__ | {{ $image.job_status }} | [Workflow Run]({{ $image.job_url}}) |
{{- end }}