<!-- This file is templated with https://pkg.go.dev/html/template -->

# HMS Build workflow dispatcher
<table>
	<tbody>
		<tr>
			<td>Github Repository</td>
			<td>Git Tag</td>
			<td>Docker image</td>
			<td>Present in CSM Releases</td>
			<td>Workflow Status</td>
			<td>Launched workflow</td>
		</tr>
{{- range $repo := .repos }}
    {{- range $i, $image := $repo.images}}
		<tr>
            {{- if eq $i 0 }}
			<td rowspan="{{len $repo.images}}">{{$repo.git_repo}}</td>
            {{- end}}
			<td><a href="https://github.com/Cray-HPE/{{ $image.git_repo }}/releases/tag/{{ $image.git_tag }}">{{ $image.git_tag }}</a></td>
			<td><a href="https://artifactory.algol60.net/ui/repos/tree/General/csm-docker%2Fstable%2F{{ $image.short_name }}%2F{{ $image.image_tag }}">{{$image.full_image}}</a></td>
			<td>
                <ul>
                {{- range $branch := $image.csm_releases }}
                    <li>{{$branch}}</li>
                {{- end }}
                </ul>
            </td>
			<td>{{$image.job_conclusion}}</td>
            {{- if $image.job_url}}
			<td><a href="{{$image.job_url}}">{{$image.workflow_name}}</a></td>
            {{ else }}
            <td>:grey_question:</td>
            {{- end -}}
		</tr>
    {{- end}}
{{- end}}
	</tbody>
</table>