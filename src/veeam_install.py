# 2022 Pure Storage Portfolio Solutions Team
# This python3 script allows user to freshly install a Veeam
# backup and replication server(VBR). This script uses a 
# pre-configured VMware vCenter template as base to install
# these Veeam components: 
#   Veeam Backup Catalog
#   Veeam Backup & Replication Server
#   Veeam Backup & Replication Console
#   Veeam Redistributable Package for Veeam Agent for Linux/Unix/Microsoft
#   Veeam 11.0.1.1261 patch.
# User needs to provide a YAML configuration file for the 
# Veeam installation to proceed. After installation, the 
# Veeam backup & replication server has the hostname as "veeam-serv1". 
# To get help menu of this script, please run "python3 veeam_install.py -h"
# to get a detailed description on how to use this script.
#

import sys
import os
import argparse
import time
import logging
import re
import ntpath
from fabric import Connection, task

import vm_operation

veeam_serv_hostname = "veeam-serv1"
veeam_serv_user = "administrator"
veeam_serv_pw = "Osmium76!"
veeam_serv_sqlinst = "veeamsql2016"    # Veeam server's SQL instance name


# run commands through ssh to VBR server
#
def run_command(cmd, conn, mylogger, timeout=180, results = None):
    try:
        mylogger.debug("Running command: %s" % cmd)
        res = conn.run(cmd, hide=True, timeout=timeout)

        if(res.stderr):
            mylogger.warning("Running command through ssh to Veeam backup server returned error: %s" % res.stderr)
            return 1

        if isinstance(results, list):
            tmp_res = (res.stdout.rstrip()).split('\n')
            for item in tmp_res:
                results.append(item)
    except Exception as exp:
        mylogger.warning("Running command through ssh to Veeam backup server caught an exception. Exception details:  %s" % exp)
        return 1

    return 0


# search the last valid 10 lines of the powershell log for success_str
#
def check_ps_log(vcdata, ps_log, success_str):
    mylogger = vcdata["mylogger"]
    encoding = ["utf-16", "utf-8"]
    log_content = []
    for item in encoding:  # the powershell output log file can be either utf-16 or utf-8 encoding.
        try:
            with open(ps_log, 'r', encoding=item) as fh:
                log_content = fh.readlines()
            break
        except (IOError, OSError) as exp:
            mylogger.warning("Hit IOError while opening file %s. Exception details: %s" % (ps_log, exp) )
            return 1
        except UnicodeDecodeError as exp:
            mylogger.debug("Hit UnicodeDecodeError while opening file %s. Exception details: %s" % (ps_log, exp) )
        except Exception as exp:
            exp_str = str(exp)
            mylogger.debug("Hit exception while opening file %s. Exception details: %s" % (ps_log, exp_str) )
            if( re.search("UTF-16 stream does not start with BOM", exp_str, re.M|re.I) != None ):
                pass
            else:
                return 1 
    if(len(log_content) == 0):
        mylogger.warning("Powershell log file %s does not have any content" % ps_log)
        return 1

    # search the last valid 10 lines of the powershell log for success_str
    count = 0 
    for i in range( len(log_content)-1, -1,-1 ):
        if(log_content[i].rstrip() == ''): 
            continue
        searchobj = re.search(r'(%s)' % success_str, log_content[i], re.M|re.I)
        if(searchobj):
            return 0
        count = count + 1
        if(count > 10):
            return 1

    return 1


# run Veeam installation powershell in the remote Veeam server
#
def run_veeam_install_ps(vcdata, veeam_serv, ps_file, ps_log, timeout, success_str):
    mylogger = vcdata["mylogger"]
    ps_log = '/' + ps_log.replace('\\', '/')    # need to convert from "C:\\temp\\veeam_bkupcatalog_install.log" to this: "/C:/temp/veeam_bkupcatalog_install.log"
    path, log_fname = os.path.split(ps_log)
    local_ps_log = f"/tmp/{log_fname}" 

    mylogger.info(f"Start running PowerShell file {ps_file} on server {veeam_serv}")
    try:
        conn = Connection(veeam_serv, user=veeam_serv_user, connect_kwargs={"password": veeam_serv_pw}, connect_timeout=600)
        conn.put(f"/tmp/{ps_file}", f"/c:/temp/{ps_file}")
    except Exception as exp:
        mylogger.warning(f"Uploading file /tmp/{ps_file} to server {veeam_serv} directory c:\\temp caught an exception. Exception details: {exp}")
        conn.close()
        return 1

    results = []
    cmd = f"powershell -File c:\\temp\\{ps_file}"

    rc = run_command(cmd, conn, mylogger, timeout, results)

    res = '\n'.join(results)
    if(rc != 0):
        mylogger.warning(f"Error: running PowerShell c:\temp\{ps_file} on server {veeam_serv} fails")
        return rc

    time.sleep(60)  # wait for 60 seconds till powershell log file is created

    try:
        conn.get(ps_log, local_ps_log)
    except Exception as exp:
        mylogger.warning(f"Downloading PowerShell log file {ps_log} to local machine as {local_ps_log} caught an exception. Exception details: {exp}")
        conn.close()
        return 1

    ps_success = check_ps_log(vcdata, local_ps_log, success_str)   # check the PowerShell log for success_str
 
    if(res == "0" and ps_success == 0 ):
        mylogger.info(f"Success: running PowerShell c:\\temp\\{ps_file} on server {veeam_serv} succeeds")
        rc = 0
    else:
        mylogger.warning(f"Error: running PowerShell c:\\temp\\{ps_file} on server {veeam_serv} fails")
        rc = 1

    conn.close()
    return rc


# install Veeam Backup Catalog
#
def install_bkup_catalog(vcdata, veeam_serv):
    mylogger = vcdata["mylogger"]
    ps_file = "veeam_bkupcatalog_install.ps1"    # powershell file name
    ps_log = "c:\\temp\\veeam_bkupcatalog_install.log"
    ps_timeout = 900
    ps_success_str = "MainEngineThread is returning 0"

    mylogger.info('-'*15 + "Start installing Veeam Backup Catalog on server %s" % veeam_serv + '-'*15)

    vbrc_service_user = f"{veeam_serv_hostname}\\{veeam_serv_user}"      # veeam-serv1\Administrator

    cmd = ("$params = '/qn', '/i', \"c:\\veeam_soft\\catalog\\VeeamBackupCatalog64.msi\", 'ACCEPTEULA=\"1\"', 'ACCEPT_THIRDPARTY_LICENSES=\"1\"', "  
           f"'VBRC_SERVICE_USER=\"{vbrc_service_user}\"', 'VBRC_SERVICE_PASSWORD=\"{veeam_serv_pw}\"', '/L*V', '{ps_log}' \n" 
            "$process = Start-Process 'msiexec.exe' -ArgumentList $params -WindowStyle Hidden -Wait -PassThru \n"
            "$process.ExitCode")

    with open(f"/tmp/{ps_file}", "w") as fh:
        fh.write(cmd)
    time.sleep(5)

    rc = run_veeam_install_ps(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str)
    if(rc == 0):
        mylogger.info("Successfully install Veeam Backup Catalog on server %s" % veeam_serv)
    else:
        mylogger.warning("Error installing Veeam Backup Catalog on server %s" % veeam_serv)

    return rc


# install Veeam Backup & Replication Server
#
def install_bkup_repl_serv(vcdata, veeam_serv):
    mylogger = vcdata["mylogger"]
    ps_file = "veeam_br_install.ps1"      # powershell file name
    ps_log = "c:\\temp\\veeam_br_install.log"
    ps_timeout = 1800
    ps_success_str = "MainEngineThread is returning 0"

    mylogger.info('-'*15 + "Start installing Veeam Backup & Replication Server on server %s" % veeam_serv + '-'*15) 

    veeam_licensefile = "c:\\veeam_soft\\veeam_license.lic"
    vbr_sqlserv = "%s\\%s" % (veeam_serv_hostname, veeam_serv_sqlinst)   # veeam-serv1\veeamsql2016 
    vbr_service_user = "%s\\%s" % (veeam_serv_hostname, veeam_serv_user) # veeam-serv1\Administrator 

    cmd = ("$params = '/qn', '/i', \"c:\\veeam_soft\\backup\\Server.x64.msi\", 'ACCEPTEULA=\"1\"', 'ACCEPT_THIRDPARTY_LICENSES=\"1\"', "
           f"'VBR_LICENSE_FILE=\"{veeam_licensefile}\"', 'VBR_SERVICE_USER=\"{vbr_service_user}\"', 'VBR_SERVICE_PASSWORD=\"{veeam_serv_pw}\"', "
           f"'VBR_SQLSERVER_SERVER=\"{vbr_sqlserv}\"', '/L*V', '{ps_log}' \n"
            "$process = Start-Process 'msiexec.exe' -ArgumentList $params -WindowStyle Hidden -Wait -PassThru \n"
            "$process.ExitCode")
    
    with open(f"/tmp/{ps_file}", "w") as fh:
        fh.write(cmd)
    time.sleep(5)

    for i in range(0,2):
        rc = run_veeam_install_ps(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str)
        if(rc == 0):
            mylogger.info("Successfully install Veeam Backup & Replication Server on server %s" % veeam_serv)
            break
        else:
            mylogger.warning("Error installing Veeam Backup & Replication Server on server %s" % veeam_serv)
            if(i == 0): 
                mylogger.warning("Re-run the Veeam Backup & Replication Server installation on server %s" % veeam_serv)
                time.sleep(60)
    
    return rc


# install Veeam Backup & Replication Console 
#
def install_bkup_repl_console(vcdata, veeam_serv):
    mylogger = vcdata["mylogger"]
    ps_file = "veeam_brconsole_install.ps1"      # powershell file name
    ps_log = "c:\\temp\\veeam_brconsole_install.log"
    ps_timeout = 1800
    ps_success_str = "MainEngineThread is returning 0"

    mylogger.info('-'*15 + "Start installing Veeam Backup & Replication Console on server %s" % veeam_serv + '-'*15)

    cmd = (f"$params = '/qn', '/i', \"c:\\veeam_soft\\backup\\Shell.x64.msi\", 'ACCEPTEULA=\"1\"', 'ACCEPT_THIRDPARTY_LICENSES=\"1\"', '/L*V', '{ps_log}' \n"
            "$process = Start-Process 'msiexec.exe' -ArgumentList $params -WindowStyle Hidden -Wait -PassThru \n"
            "$process.ExitCode")

    with open(f"/tmp/{ps_file}", "w") as fh:
        fh.write(cmd)
    time.sleep(5)

    rc = run_veeam_install_ps(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str)
    if(rc == 0):
        mylogger.info("Successfully install Veeam Backup & Replication Console on server %s" % veeam_serv)
    else:
        mylogger.warning("Error installing Veeam Backup & Replication Console on server %s" % veeam_serv)
    
    return rc


def install_service_pkgs(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str, service, install_msi):
    mylogger = vcdata["mylogger"]
    mylogger.info('-'*15 + "Start installing %s on server %s" % (service, veeam_serv) + '-'*15)

    cmd = (f"$params = '/qn', '/i', \"{install_msi}\", 'ACCEPTEULA=\"1\"', 'ACCEPT_THIRDPARTY_LICENSES=\"1\"', '/L*V', '{ps_log}' \n"
            "$process = Start-Process 'msiexec.exe' -ArgumentList $params -WindowStyle Hidden -Wait -PassThru \n"
            "$process.ExitCode")

    with open(f"/tmp/{ps_file}", "w") as fh:
        fh.write(cmd)
    time.sleep(5)

    rc = run_veeam_install_ps(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str)
    if(rc == 0):
        mylogger.info("Successfully install %s on server %s" % (service, veeam_serv) )
    else:
        mylogger.warning("Error installing %s on server %s" % (service, veeam_serv) )
    
    return rc


def install_veeam_service_pkgs(vcdata, veeam_serv):
    
    # install Veeam Mount Service
    ps_file = "veeam_mountserv_install.ps1"      # powershell file name
    ps_log = "c:\\temp\\veeam_mountserv_install.log"
    ps_timeout = 1800
    ps_success_str = "MainEngineThread is returning 0"
    service = "Veeam Mount Service"
    install_msi = "c:\\veeam_soft\\packages\\VeeamMountService.msi"

    rc = install_service_pkgs(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str, service, install_msi)
    if(rc != 0):
        return rc

    # install Veeam Distribution Service
    ps_file = "veeam_distribserv_install.ps1"      # powershell file name
    ps_log = "c:\\temp\\veeam_distribserv_install.log"
    ps_timeout = 1800
    ps_success_str = "MainEngineThread is returning 0"
    service = "Veeam Distribution Service"
    install_msi = "c:\\veeam_soft\\packages\\VeeamDistributionSvc.msi"

    rc = install_service_pkgs(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str, service, install_msi)
    if(rc != 0):
        return rc

    # install Veeam Backup Transport 
    ps_file = "veeam_transport_install.ps1"      # powershell file name
    ps_log = "c:\\temp\\veeam_transport_install.log"
    ps_timeout = 1800
    ps_success_str = "MainEngineThread is returning 0"
    service = "Veeam Backup Transport"
    install_msi = "c:\\veeam_soft\\packages\\VeeamTransport.msi"

    rc = install_service_pkgs(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str, service, install_msi)
    if(rc != 0):
        return rc

    # install Veeam Agent for Linux Redistributable
    ps_file = "veeam_agtlnxredist_install.ps1"      # powershell file name
    ps_log = "c:\\temp\\veeam_agtlnxredist_install.log"
    ps_timeout = 1800
    ps_success_str = "MainEngineThread is returning 0"
    service = "Veeam Agent for Linux Redistributable"
    install_msi = "c:\\veeam_soft\\packages\\VALRedist.msi"

    rc = install_service_pkgs(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str, service, install_msi)
    if(rc != 0):
        return rc

    # install Veeam Agent for Unix Redistributable
    ps_file = "veeam_agtunxredist_install.ps1"      # powershell file name
    ps_log = "c:\\temp\\veeam_agtunxredist_install.log"
    ps_timeout = 1800
    ps_success_str = "MainEngineThread is returning 0"
    service = "Veeam Agent for Unix Redistributable"
    install_msi = "c:\\veeam_soft\\packages\\VAURedist.msi"

    rc = install_service_pkgs(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str, service, install_msi)
    if(rc != 0):
        return rc

    # install Veeam Agent for Windows Redistributable
    ps_file = "veeam_agtwinredist_install.ps1"      # powershell file name
    ps_log = "c:\\temp\\veeam_agtwinredist_install.log"
    ps_timeout = 1800
    ps_success_str = "MainEngineThread is returning 0"
    service = "Veeam Agent for Windows Redistributable"
    install_msi = "c:\\veeam_soft\\packages\\VAWRedist.msi"

    rc = install_service_pkgs(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str, service, install_msi)
    if(rc != 0):
        return rc

    return 0


# install Veeam Patch version veeam_backup_11.0.1.1261_CumulativePatch20220302.exe
#
def install_patch(vcdata, veeam_serv):
    mylogger = vcdata["mylogger"]
    patch_file = "c:\\veeam_soft\\updates\\veeam_backup_11.0.1.1261_CumulativePatch20220302.exe"
    ps_file = "veeam_patch_install.ps1"      # powershell file name
    ps_log = "c:\\temp\\veeam_patch_install.log"
    ps_timeout = 1800
    ps_success_str = "Return value 0"

    mylogger.info('-'*10 + "Start installing Veeam Patch \"veeam_backup_11.0.1.1261_CumulativePatch20220302\" on server %s" % veeam_serv + '-'*10)

    cmd = (f"$params = '/silent', '/noreboot', 'VBR_AUTO_UPGRADE=\"0\"', '/log', '{ps_log}' \n"
           f"$process = Start-Process '{patch_file}' -ArgumentList $params -WindowStyle Hidden -Wait -PassThru \n"
            "$process.ExitCode")

    with open(f"/tmp/{ps_file}", "w") as fh:
        fh.write(cmd)
    time.sleep(5)

    rc = run_veeam_install_ps(vcdata, veeam_serv, ps_file, ps_log, ps_timeout, ps_success_str)
    if(rc == 0):
        mylogger.info("Successfully install Veeam Patch \"veeam_backup_11.0.1.1261_CumulativePatch20220302\" on server %s" % veeam_serv)
    else:
        mylogger.warning("Error installing Veeam Patch \"veeam_backup_11.0.1.1261_CumulativePatch20220302\" on server %s" % veeam_serv)

    return rc


# For Veeam server, after installing patch, update the server components. After that, create a new registry value
#
def update_server_component(vcdata, veeam_serv):
    mylogger = vcdata["mylogger"]
    ps_file = "veeam_update_servcomponent.ps1"
    ps_timeout = 1800

    mylogger.info('-'*10 + "Start updating Veeam Backup & Replication server components" + '-'*10)

    cmd = "shutdown -r -t 5"
    try:
        conn = Connection(veeam_serv, user=veeam_serv_user, connect_kwargs={"password": veeam_serv_pw}, connect_timeout=600)
        rc = run_command(cmd, conn, mylogger, ps_timeout)
        if(rc != 0):
            mylogger.warning(f"Error: unable to reboot Veeam Backup & Replication server {veeam_serv}")
    except Exception as exp:
        mylogger.warning(f"Unable to establish connection to Veeam backup server {veeam_serv}. Exception details: {exp}")
        rc = 1 
    finally:
        conn.close()
        if(rc != 0): return rc

    time.sleep(300)
    # reboot the VBR server for its Veeam powershell components to fully work

    cmd = ("$ProgressPreference = \"SilentlyContinue\" \n"
           "Update-VBRServerComponent \n"
           "New-ItemProperty -Path \"HKLM:\\SOFTWARE\\Veeam\\Veeam Backup and Replication\" -Name MaxSnapshotsPerDatastore -Value 100 -PropertyType DWord")

    with open(f"/tmp/{ps_file}", "w") as fh:
        fh.write(cmd)
    time.sleep(5)

    try:
        conn = Connection(veeam_serv, user=veeam_serv_user, connect_kwargs={"password": veeam_serv_pw}, connect_timeout=600)
        conn.put(f"/tmp/{ps_file}", f"/c:/temp/{ps_file}")
    except Exception as exp:
        mylogger.warning(f"Uploading file /tmp/{ps_file} to server {veeam_serv} directory c:\\temp caught an exception. Exception details: {exp}")
        conn.close()
        return 1

    results = []
    cmd = f"powershell -File c:\\temp\\{ps_file}"

    rc = run_command(cmd, conn, mylogger, ps_timeout, results)
    conn.close()

    res = '\n'.join(results)
    if(rc != 0):
        mylogger.warning(f"Error: running PowerShell c:\temp\{ps_file} on server {veeam_serv} fails")
        return rc
    else: 
        mylogger.info(f"Successfully update Veeam Backup & Replication server components for {veeam_serv}")
        mylogger.info(f"Successfully update Veeam Backup & Replication server {veeam_serv} registry.")
        return 0


# start installing Veeam backup and replication
#
def start_install_vbr(yamlfile, mylogger, deplogfile):
    yaml_section = "veeam_install"
    vcdata = {}

    (rc, vcdata) = vm_operation.create_from_yaml(yamlfile, yaml_section, mylogger, deplogfile)
    if(rc != 0): return rc

    vcdata["mylogger"] = mylogger

    deployed_vm = [ vm["vm_name"] for vm in vcdata["deployed_vm"] ]
    veeam_servs = ', '.join(deployed_vm)
    mylogger.info("Successfully deployed virtual machine %s as Veeam Backup & Replication Server" % veeam_servs)
     
    # get deployed veeam servers ip addresses
    veeam_servers = vm_operation.get_vm_ip(vc_name = vcdata["vcenter_name"], vc_user = vcdata["vcenter_user"], vc_pw = vcdata["vcenter_pw"], 
                                           vc_ssl_check = vcdata["ssl-check"], mylogger = mylogger, vm_list = deployed_vm)

    return_rc = 0
    for server in veeam_servers:
        veeam_serv = server["vm_ip"]

        mylogger.info("Start installing Veeam Backup & Replication on server %s" % veeam_serv)
        rc = install_bkup_catalog(vcdata, veeam_serv)
        if(rc != 0):
            return_rc = rc
            continue

        rc = install_bkup_repl_serv(vcdata, veeam_serv)
        if(rc != 0):
            return_rc = rc
            continue

        rc = install_bkup_repl_console(vcdata, veeam_serv)
        if(rc != 0):
            return_rc = rc
            continue
  
        rc = install_veeam_service_pkgs(vcdata, veeam_serv)
        if(rc != 0):
            return_rc = rc
            continue

        rc = install_patch(vcdata, veeam_serv)
        if(rc != 0):
            return_rc = rc
            continue
 
        rc = update_server_component(vcdata, veeam_serv)
        if(rc != 0):
            return_rc = rc
            continue

        mylogger.info("Successfully complete Veeam Backup & Replication installation on server %s" % veeam_serv)

    return return_rc

def main(argv):
    parser = argparse.ArgumentParser()

    parser.add_argument('-yf', '--yamlfile', required=True, help='YAML file', dest='yamlfile', type=str)
    parser.add_argument('-lg', '--loglevel', required=False, help='Log Level. Default is INFO', dest='loglevel', type=str)
    parser.add_argument('-of', '--outlogfile', required=False, help='Output log file', dest='outlogfile', type=str)

    args = parser.parse_args()
    yamlfile = args.yamlfile

    # get logging level
    loglevel_dict = {'DEBUG':logging.DEBUG, 'INFO':logging.INFO, 'WARNING':logging.WARNING}

    if not args.loglevel:
        loglevel = logging.INFO
    elif args.loglevel in loglevel_dict:
        loglevel = loglevel_dict[args.loglevel]
    else:
        print('The input loglevel is not right. Please choose among DEBUG, INFO, WARNING')
        return 1

    if not args.outlogfile:
        outlogfile = 'veeam_install_' + time.strftime('%Y_%m_%d_%H_%M_%S', time.localtime()) + '.log'
    else:
        outlogfile = args.outlogfile

    deplogfile = outlogfile + '_dep'

    # configure logger and logging level
    mylogger = logging.getLogger(__name__)
    mylogger.setLevel(loglevel)

    file_handle = logging.FileHandler(outlogfile)
    file_handle.setLevel(loglevel)

    stream_handle = logging.StreamHandler()
    stream_handle.setLevel(loglevel)

    formatter = logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)s {%(module)s} [%(funcName)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    file_handle.setFormatter(formatter)
    stream_handle.setFormatter(formatter)
    mylogger.addHandler(file_handle)
    mylogger.addHandler(stream_handle)

    mylogger.propagate = False

    # start operation
    mylogger.info("Start installing Veeam backup and replication server")

    rc = start_install_vbr(yamlfile, mylogger, deplogfile)
    return rc

if __name__ == "__main__":
    rc = main(sys.argv[1:])
    sys.exit(rc)

# made some changes on 2:46 AM, 10/29 on "br1" bramch
