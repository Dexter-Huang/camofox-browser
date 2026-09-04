#!/usr/bin/env pwsh
<##
.SYNOPSIS
    Build the Python Camofox sidecar image.

.DESCRIPTION
    The fixed Camoufox archive must already exist under bin/. The Dockerfile
    verifies its SHA-256 and installs it without network downloads.
##>

param(
    [ValidateSet('build', 'up', 'down', 'clean')]
    [string]$Target = 'build',
    [string]$ImageTag = 'geo-camofox-browser:local',
    [string]$ContainerName = 'geo-camofox-browser',
    [int]$HostPort = 9377
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSCommandPath
Set-Location $ProjectRoot

switch ($Target) {
    'build' {
        docker build -t $ImageTag .
    }
    'up' {
        if (-not (docker image inspect $ImageTag 2>$null)) { docker build -t $ImageTag . }
        docker rm -f $ContainerName 2>$null | Out-Null
        docker run -d --restart unless-stopped --name $ContainerName -p "${HostPort}:9377" $ImageTag | Out-Null
    }
    'down' {
        docker rm -f $ContainerName 2>$null | Out-Null
    }
    'clean' {
        docker image rm $ImageTag 2>$null | Out-Null
    }
}
