# Authors:
#     Matthew Harmsen <mharmsen@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright (C) 2011 Red Hat, Inc.
# All rights reserved.
#

# System Imports
from __future__ import absolute_import
from __future__ import print_function
import sys
import signal
import subprocess

if not hasattr(sys, "hexversion") or sys.hexversion < 0x020700f0:
    print("Python version %s.%s.%s is too old." % sys.version_info[:3])
    print("Please upgrade to at least Python 2.7.0.")
    sys.exit(1)
try:
    import fileinput
    import ldap
    import os
    import requests
    import traceback
    import pki
    from pki.server.deployment import pkiconfig as config
    from pki.server.deployment import pkimanifest as manifest
    from pki.server.deployment.pkihelper import RETRYABLE_EXCEPTIONS
    from pki.server.deployment.pkiparser import PKIConfigParser
    from pki.server.deployment import pkilogging
    from pki.server.deployment import pkimessages as log
except ImportError:
    print("""\
There was a problem importing one of the required Python modules. The
error was:

    %s
""" % sys.exc_info()[1], file=sys.stderr)
    sys.exit(1)


deployer = pki.server.deployment.PKIDeployer()


# Handle the Keyboard Interrupt
# pylint: disable=W0613
def interrupt_handler(event, frame):
    print()
    print('\nInstallation canceled.')
    sys.exit(1)


# PKI Deployment Functions
def main(argv):
    """main entry point"""

    config.pki_deployment_executable = os.path.basename(argv[0])

    # Set the umask
    os.umask(config.PKI_DEPLOYMENT_DEFAULT_UMASK)

    # Read and process command-line arguments.
    parser = PKIConfigParser(
        'PKI Instance Installation and Configuration',
        log.PKISPAWN_EPILOG,
        deployer=deployer)

    parser.optional.add_argument(
        '-f',
        dest='user_deployment_cfg', action='store',
        nargs=1, metavar='<file>',
        help='configuration filename '
        '(MUST specify complete path)')

    parser.optional.add_argument(
        '--precheck',
        dest='precheck', action='store_true',
        help='Execute pre-checks and exit')

    parser.optional.add_argument(
        '--skip-configuration',
        dest='skip_configuration',
        action='store_true',
        help='skip configuration step')

    parser.optional.add_argument(
        '--skip-installation',
        dest='skip_installation',
        action='store_true',
        help='skip installation step')

    parser.optional.add_argument(
        '--enforce-hostname',
        dest='enforce_hostname',
        action='store_true',
        help='enforce strict hostname/FQDN checks')

    args = parser.process_command_line_arguments()

    config.default_deployment_cfg = \
        config.PKI_DEPLOYMENT_DEFAULT_CONFIGURATION_FILE

    # -f <user deployment config>
    if args.user_deployment_cfg is not None:
        config.user_deployment_cfg = str(
            args.user_deployment_cfg).strip('[\']')

    parser.validate()

    # Currently the only logic in deployer's validation is the
    # hostname check; at some point this might need to be updated.
    if args.enforce_hostname:
        deployer.validate()

    interactive = False

    if config.user_deployment_cfg is None:
        interactive = True
        parser.indent = 0
        print(log.PKISPAWN_INTERACTIVE_INSTALLATION)
    else:
        sanitize_user_deployment_cfg(config.user_deployment_cfg)

    # Only run this program as "root".
    if not os.geteuid() == 0:
        sys.exit("'%s' must be run as root!" % argv[0])

    while True:
        # -s <subsystem>
        if args.pki_subsystem is None:
            interactive = True
            parser.indent = 0

            deployer.subsystem_name = parser.read_text(
                'Subsystem (CA/KRA/OCSP/TKS/TPS)',
                options=['CA', 'KRA', 'OCSP', 'TKS', 'TPS'],
                default='CA', case_sensitive=False).upper()
            print()
        else:
            deployer.subsystem_name = str(args.pki_subsystem).strip('[\']')

        parser.init_config()

        if config.user_deployment_cfg is None:
            if args.precheck:
                sys.exit(
                    'precheck mode is only valid for non-interactive installs')
            interactive = True
            parser.indent = 2

            print("Tomcat:")
            instance_name = parser.read_text(
                'Instance', 'DEFAULT', 'pki_instance_name')
            existing_data = parser.read_existing_deployment_data(instance_name)

            set_port(parser,
                     'pki_http_port',
                     'HTTP port',
                     existing_data)

            set_port(parser,
                     'pki_https_port',
                     'Secure HTTP port',
                     existing_data)

            set_port(parser,
                     'pki_ajp_port',
                     'AJP port',
                     existing_data)

            set_port(parser,
                     'pki_tomcat_server_port',
                     'Management port',
                     existing_data)

            print()

            print("Administrator:")
            parser.read_text('Username', deployer.subsystem_name, 'pki_admin_uid')

            admin_password = parser.read_password(
                'Password', deployer.subsystem_name, 'pki_admin_password',
                verifyMessage='Verify password')

            parser.set_property(deployer.subsystem_name, 'pki_backup_password',
                                admin_password)
            parser.set_property(deployer.subsystem_name,
                                'pki_client_database_password',
                                admin_password)
            parser.set_property(deployer.subsystem_name,
                                'pki_client_pkcs12_password',
                                admin_password)

            if parser.mdict['pki_import_admin_cert'] == 'True':
                import_cert = 'Y'
            else:
                import_cert = 'N'

            import_cert = parser.read_text(
                'Import certificate (Yes/No)',
                default=import_cert, options=['Yes', 'Y', 'No', 'N'],
                sign='?', case_sensitive=False).lower()

            if import_cert == 'y' or import_cert == 'yes':
                parser.set_property(deployer.subsystem_name,
                                    'pki_import_admin_cert',
                                    'True')
                parser.read_text('Import certificate from',
                                 deployer.subsystem_name,
                                 'pki_admin_cert_file')
            else:
                parser.set_property(deployer.subsystem_name,
                                    'pki_import_admin_cert',
                                    'False')

                parser.read_text('Export certificate to',
                                 deployer.subsystem_name,
                                 'pki_client_admin_cert')

            # if parser.mdict['pki_hsm_enable'] == 'True':
            #     use_hsm = 'Y'
            # else:
            #     use_hsm = 'N'

            # use_hsm = parser.read_text(
            #     'Using hardware security module (HSM) (Yes/No)',
            #     default=use_hsm, options=['Yes', 'Y', 'No', 'N'],
            #     sign='?', case_sensitive=False).lower()

            # if use_hsm == 'y' or use_hsm == 'yes':
            #     # XXX:  Suppress interactive HSM installation
            #     print "Interactive HSM installation is currently unsupported."
            #     sys.exit(0)

            # TBD:  Interactive HSM installation
            # parser.set_property(deployer.subsystem_name,
            #                     'pki_hsm_enable',
            #                     'True')
            # modulename = parser.read_text(
            #     'HSM Module Name (e. g. - nethsm)', allow_empty=False)
            # parser.set_property(deployer.subsystem_name,
            #                     'pki_hsm_modulename',
            #                     modulename)
            # libfile = parser.read_text(
            #     'HSM Lib File ' +
            #     '(e. g. - /opt/nfast/toolkits/pkcs11/libcknfast.so)',
            #     allow_empty=False)
            # parser.set_property(deployer.subsystem_name,
            #                     'pki_hsm_libfile',
            #                     libfile)
            print()

            print("Directory Server:")
            while True:
                parser.read_text('Hostname',
                                 deployer.subsystem_name,
                                 'pki_ds_hostname')

                if parser.mdict['pki_ds_secure_connection'] == 'True':
                    secure = 'Y'
                else:
                    secure = 'N'

                secure = parser.read_text(
                    'Use a secure LDAPS connection (Yes/No/Quit)',
                    default=secure,
                    options=['Yes', 'Y', 'No', 'N', 'Quit', 'Q'],
                    sign='?', case_sensitive=False).lower()

                if secure == 'q' or secure == 'quit':
                    print("Installation canceled.")
                    sys.exit(0)

                if secure == 'y' or secure == 'yes':
                    # Set secure DS connection to true
                    parser.set_property(deployer.subsystem_name,
                                        'pki_ds_secure_connection',
                                        'True')
                    # Prompt for secure 'ldaps' port
                    parser.read_text('Secure LDAPS Port',
                                     deployer.subsystem_name,
                                     'pki_ds_ldaps_port')
                    # Specify complete path to a directory server
                    # CA certificate pem file
                    pem_file = parser.read_text(
                        'Directory Server CA certificate pem file',
                        allow_empty=False)
                    parser.set_property(deployer.subsystem_name,
                                        'pki_ds_secure_connection_ca_pem_file',
                                        pem_file)
                else:
                    parser.read_text('LDAP Port',
                                     deployer.subsystem_name,
                                     'pki_ds_ldap_port')

                parser.read_text('Bind DN',
                                 deployer.subsystem_name,
                                 'pki_ds_bind_dn')
                parser.read_password('Password',
                                     deployer.subsystem_name,
                                     'pki_ds_password')

                try:
                    parser.ds_verify_configuration()

                except ldap.LDAPError as e:
                    parser.print_text('ERROR: ' + e.args[0]['desc'])
                    continue

                parser.read_text('Base DN',
                                 deployer.subsystem_name,
                                 'pki_ds_base_dn')
                try:
                    if not parser.ds_base_dn_exists():
                        break

                except ldap.LDAPError as e:
                    parser.print_text('ERROR: ' + e.args[0]['desc'])
                    continue

                remove = parser.read_text(
                    'Base DN already exists. Overwrite (Yes/No/Quit)',
                    options=['Yes', 'Y', 'No', 'N', 'Quit', 'Q'],
                    sign='?', allow_empty=False, case_sensitive=False).lower()

                if remove == 'q' or remove == 'quit':
                    print("Installation canceled.")
                    sys.exit(0)

                if remove == 'y' or remove == 'yes':
                    break

            print()

            print("Security Domain:")

            if deployer.subsystem_name == "CA":
                parser.read_text('Name',
                                 deployer.subsystem_name,
                                 'pki_security_domain_name')

            else:
                while True:
                    parser.read_text('Hostname',
                                     deployer.subsystem_name,
                                     'pki_security_domain_hostname')

                    parser.read_text('Secure HTTP port',
                                     deployer.subsystem_name,
                                     'pki_security_domain_https_port')

                    try:
                        parser.sd_connect()
                        info = parser.sd_get_info()
                        parser.print_text('Name: ' + info.name)
                        parser.set_property(deployer.subsystem_name,
                                            'pki_security_domain_name',
                                            info.name)
                        break
                    except RETRYABLE_EXCEPTIONS as e:
                        parser.print_text('ERROR: ' + str(e))

                while True:
                    parser.read_text('Username',
                                     deployer.subsystem_name,
                                     'pki_security_domain_user')
                    parser.read_password('Password',
                                         deployer.subsystem_name,
                                         'pki_security_domain_password')

                    try:
                        parser.sd_authenticate()
                        break
                    except requests.exceptions.HTTPError as e:
                        parser.print_text('ERROR: ' + str(e))

            print()

            if deployer.subsystem_name == "TPS":
                print("External Servers:")

                while True:
                    parser.read_text('CA URL',
                                     deployer.subsystem_name,
                                     'pki_ca_uri')
                    try:
                        status = parser.get_server_status('ca', 'pki_ca_uri')
                        if status == 'running':
                            break
                        parser.print_text('ERROR: CA is not running')
                    except RETRYABLE_EXCEPTIONS as e:
                        parser.print_text('ERROR: ' + str(e))

                while True:
                    parser.read_text('TKS URL',
                                     deployer.subsystem_name,
                                     'pki_tks_uri')
                    try:
                        status = parser.get_server_status('tks', 'pki_tks_uri')
                        if status == 'running':
                            break
                        parser.print_text('ERROR: TKS is not running')
                    except RETRYABLE_EXCEPTIONS as e:
                        parser.print_text('ERROR: ' + str(e))

                while True:
                    keygen = parser.read_text(
                        'Enable server side key generation (Yes/No)',
                        options=['Yes', 'Y', 'No', 'N'], default='N',
                        sign='?', case_sensitive=False).lower()

                    if keygen == 'y' or keygen == 'yes':
                        parser.set_property(deployer.subsystem_name,
                                            'pki_enable_server_side_keygen',
                                            'True')

                        parser.read_text('KRA URL',
                                         deployer.subsystem_name,
                                         'pki_kra_uri')
                        try:
                            status = parser.get_server_status(
                                'kra', 'pki_kra_uri')
                            if status == 'running':
                                break
                            parser.print_text('ERROR: KRA is not running')
                        except RETRYABLE_EXCEPTIONS as e:
                            parser.print_text('ERROR: ' + str(e))
                    else:
                        parser.set_property(deployer.subsystem_name,
                                            'pki_enable_server_side_keygen',
                                            'False')
                        break

                print()

                print("Authentication Database:")

                while True:
                    parser.read_text('Hostname',
                                     deployer.subsystem_name,
                                     'pki_authdb_hostname')
                    parser.read_text('Port',
                                     deployer.subsystem_name,
                                     'pki_authdb_port')
                    basedn = parser.read_text('Base DN', allow_empty=False)
                    parser.set_property(deployer.subsystem_name,
                                        'pki_authdb_basedn',
                                        basedn)

                    try:
                        parser.authdb_connect()
                        if parser.authdb_base_dn_exists():
                            break
                        else:
                            parser.print_text('ERROR: base DN does not exist')

                    except ldap.LDAPError as e:
                        parser.print_text('ERROR: ' + e.args[0]['desc'])

                print()

        if interactive:
            parser.indent = 0

            begin = parser.read_text(
                'Begin installation (Yes/No/Quit)',
                options=['Yes', 'Y', 'No', 'N', 'Quit', 'Q'],
                sign='?', allow_empty=False, case_sensitive=False).lower()
            print()

            if begin == 'q' or begin == 'quit':
                print("Installation canceled.")
                sys.exit(0)

            if begin == 'y' or begin == 'yes':
                break

        else:
            break

    if not os.path.exists(config.PKI_DEPLOYMENT_SOURCE_ROOT +
                          "/" + deployer.subsystem_name.lower()):
        print("ERROR:  " + log.PKI_SUBSYSTEM_NOT_INSTALLED_1 %
              deployer.subsystem_name.lower())
        sys.exit(1)

    start_logging()

    # Read the specified PKI configuration file.
    rv = parser.read_pki_configuration_file()
    if rv != 0:
        config.pki_log.error(log.PKI_UNABLE_TO_PARSE_1, rv,
                             extra=config.PKI_INDENTATION_LEVEL_0)
        sys.exit(1)

    # --skip-configuration
    if args.skip_configuration:
        parser.set_property(deployer.subsystem_name,
                            'pki_skip_configuration', 'True')

    # --skip-installation
    if args.skip_installation:
        parser.set_property(deployer.subsystem_name,
                            'pki_skip_installation', 'True')

    create_master_dictionary(parser)

    if not interactive and \
            not config.str2bool(parser.mdict['pki_skip_configuration']):
        check_ds(parser)
        check_security_domain(parser)

    if args.precheck:
        print('pre-checks completed successfully.')
        sys.exit(0)

    print("Installing " + deployer.subsystem_name + " into " +
          parser.mdict['pki_instance_path'] + ".")

    # Process the various "scriptlets" to create the specified PKI subsystem.
    pki_subsystem_scriptlets = parser.mdict['spawn_scriplets'].split()
    deployer.init(parser)

    try:
        for scriptlet_name in pki_subsystem_scriptlets:

            scriptlet_module = __import__(
                "pki.server.deployment.scriptlets." + scriptlet_name,
                fromlist=[scriptlet_name])

            scriptlet = scriptlet_module.PkiScriptlet()

            scriptlet.spawn(deployer)

    except subprocess.CalledProcessError as e:
        log_error_details()
        print()
        print("Installation failed: Command failed: %s" % ' '.join(e.cmd))
        if e.output:
            print(e.output)
        print()
        print('Please check pkispawn logs in %s/%s' % (config.pki_log_dir, config.pki_log_name))
        sys.exit(1)

    except requests.HTTPError as e:
        r = e.response
        print()

        print('Installation failed:')
        if r.headers['content-type'] == 'application/json':
            data = r.json()
            print('%s: %s' % (data['ClassName'], data['Message']))
        else:
            print(r.text)

        print()
        print('Please check the %s logs in %s.' %
              (deployer.subsystem_name, deployer.mdict['pki_subsystem_log_path']))

        sys.exit(1)

    except Exception as e:  # pylint: disable=broad-except
        log_error_details()
        print()
        print("Installation failed: %s" % e)
        print()
        sys.exit(1)

    # ALWAYS archive configuration file and manifest file

    config.pki_log.info(
        log.PKI_ARCHIVE_CONFIG_MESSAGE_1,
        deployer.mdict['pki_user_deployment_cfg_spawn_archive'],
        extra=config.PKI_INDENTATION_LEVEL_1)

    # For debugging/auditing purposes, save a timestamped copy of
    # this configuration file in the subsystem archive
    deployer.file.copy(
        deployer.mdict['pki_user_deployment_cfg_replica'],
        deployer.mdict['pki_user_deployment_cfg_spawn_archive'])

    config.pki_log.info(
        log.PKI_ARCHIVE_MANIFEST_MESSAGE_1,
        deployer.mdict['pki_manifest_spawn_archive'],
        extra=config.PKI_INDENTATION_LEVEL_1)

    # for record in manifest.database:
    #     print tuple(record)

    manifest_file = manifest.File(deployer.manifest_db)
    manifest_file.register(deployer.mdict['pki_manifest'])
    manifest_file.write()

    deployer.file.modify(deployer.mdict['pki_manifest'], silent=True)

    # Also, for debugging/auditing purposes, save a timestamped copy of
    # this installation manifest file
    deployer.file.copy(
        deployer.mdict['pki_manifest'],
        deployer.mdict['pki_manifest_spawn_archive'])

    external = deployer.configuration_file.external
    standalone = deployer.configuration_file.standalone
    step_one = deployer.configuration_file.external_step_one
    skip_configuration = deployer.configuration_file.skip_configuration

    if skip_configuration:
        print_skip_configuration_information(parser.mdict)

    elif (external or standalone) and step_one:
        if deployer.subsystem_name == 'CA':
            print_external_ca_step_one_information(parser.mdict)

        elif deployer.subsystem_name == 'KRA':
            print_kra_step_one_information(parser.mdict)

        else:  # OCSP
            print_ocsp_step_one_information(parser.mdict)

    else:
        print_final_install_information(parser.mdict)


def sanitize_user_deployment_cfg(cfg):

    # Correct any section headings in the user's configuration file
    for line in fileinput.FileInput(cfg, inplace=1):
        # Remove extraneous leading and trailing whitespace from all lines
        line = line.strip()
        # Normalize section headings to match '/usr/share/pki/server/etc/default.cfg'
        if line.startswith("["):
            if line.upper().startswith("[DEFAULT"):
                line = "[DEFAULT]"
            elif line.upper().startswith("[TOMCAT"):
                line = "[Tomcat]"
            elif line.upper().startswith("[CA"):
                line = "[CA]"
            elif line.upper().startswith("[KRA"):
                line = "[KRA]"
            elif line.upper().startswith("[OCSP"):
                line = "[OCSP]"
            elif line.upper().startswith("[RA"):
                line = "[RA]"
            elif line.upper().startswith("[TKS"):
                line = "[TKS]"
            elif line.upper().startswith("[TPS"):
                line = "[TPS]"
            else:
                # Notify user of the existence of an invalid section heading
                sys.stderr.write("'%s' contains an invalid section "
                                 "heading called '%s'!\n" % (cfg, line))
        print(line)


def start_logging():
    # Enable 'pkispawn' logging.
    config.pki_log_dir = config.PKI_DEPLOYMENT_LOG_ROOT
    config.pki_log_name = "pki" + "-" + \
                          deployer.subsystem_name.lower() + \
                          "-" + "spawn" + "." + \
                          deployer.log_timestamp + "." + "log"
    print('Log file: %s/%s' % (config.pki_log_dir, config.pki_log_name))

    pkilogging.enable_pki_logger(config.pki_log_dir,
                                 config.pki_log_name,
                                 config.pki_log_level,
                                 config.pki_console_log_level,
                                 "pkispawn")


def create_master_dictionary(parser):

    # Read in the PKI slots configuration file.
    parser.compose_pki_slots_dictionary()

    # Combine the various sectional dictionaries into a PKI master dictionary
    parser.compose_pki_master_dictionary()

    parser.mdict['pki_spawn_log'] = \
        config.pki_log_dir + "/" + config.pki_log_name

    config.pki_log.debug(log.PKI_DICTIONARY_MASTER,
                         extra=config.PKI_INDENTATION_LEVEL_0)
    config.pki_log.debug(pkilogging.log_format(parser.mdict),
                         extra=config.PKI_INDENTATION_LEVEL_0)


def check_security_domain(parser):
    if parser.mdict['pki_security_domain_type'] != "new":
        try:
            # Verify existence of Security Domain Password
            if 'pki_security_domain_password' not in parser.mdict or \
                    not len(parser.mdict['pki_security_domain_password']):
                config.pki_log.error(
                    log.PKIHELPER_UNDEFINED_CONFIGURATION_FILE_ENTRY_2,
                    "pki_security_domain_password",
                    parser.mdict['pki_user_deployment_cfg'],
                    extra=config.PKI_INDENTATION_LEVEL_0)
                sys.exit(1)

            if not config.str2bool(parser.mdict['pki_skip_sd_verify']):
                parser.sd_connect()
                info = parser.sd_get_info()
                parser.set_property(deployer.subsystem_name,
                                    'pki_security_domain_name',
                                    info.name)
                parser.sd_authenticate()

        except requests.exceptions.RequestException as e:
            print(('ERROR:  Unable to access security domain: ' + str(e)))
            sys.exit(1)


def check_ds(parser):
    try:
        # Verify existence of Directory Server Password
        if 'pki_ds_password' not in parser.mdict or \
                not len(parser.mdict['pki_ds_password']):
            config.pki_log.error(
                log.PKIHELPER_UNDEFINED_CONFIGURATION_FILE_ENTRY_2,
                "pki_ds_password",
                parser.mdict['pki_user_deployment_cfg'],
                extra=config.PKI_INDENTATION_LEVEL_0)
            sys.exit(1)

        if not config.str2bool(parser.mdict['pki_skip_ds_verify']):
            parser.ds_verify_configuration()

            if parser.ds_base_dn_exists() and not \
                    config.str2bool(parser.mdict['pki_ds_remove_data']):
                print('ERROR:  Base DN already exists.')
                sys.exit(1)

    except ldap.LDAPError as e:
        server = parser.protocol + '://' + parser.hostname + ':' + parser.port
        print('ERROR:  Unable to access directory server (' + server + '): ' +
              e.args[0]['desc'])
        sys.exit(1)


def set_port(parser, tag, prompt, existing_data):
    if tag in existing_data:
        parser.set_property(deployer.subsystem_name, tag, existing_data[tag])
    else:
        parser.read_text(prompt, deployer.subsystem_name, tag)


def print_external_ca_step_one_information(mdict):

    print(log.PKI_SPAWN_INFORMATION_HEADER)
    print("      The %s subsystem of the '%s' instance is still incomplete." %
          (deployer.subsystem_name, mdict['pki_instance_name']))
    print()
    print("      NSS database: %s" % mdict['pki_server_database_path'])
    print()

    signing_csr = mdict['pki_ca_signing_csr_path']

    if signing_csr:
        print("      A CSR for the CA signing certificate has been generated in:")
        print("            %s" % mdict['pki_ca_signing_csr_path'])
    else:
        print("      No CSR has been generated for CA signing certificate.")

    print(log.PKI_RUN_INSTALLATION_STEP_TWO)
    print(log.PKI_SPAWN_INFORMATION_FOOTER)


def print_kra_step_one_information(mdict):

    print(log.PKI_SPAWN_INFORMATION_HEADER)
    print("      The %s subsystem of the '%s' instance is still incomplete." %
          (deployer.subsystem_name, mdict['pki_instance_name']))
    print()
    print("      NSS database: %s" % mdict['pki_server_database_path'])
    print()

    storage_csr = mdict['pki_storage_csr_path']
    transport_csr = mdict['pki_transport_csr_path']
    subsystem_csr = mdict['pki_subsystem_csr_path']
    sslserver_csr = mdict['pki_sslserver_csr_path']
    audit_csr = mdict['pki_audit_signing_csr_path']
    admin_csr = mdict['pki_admin_csr_path']

    if storage_csr or transport_csr or subsystem_csr or sslserver_csr \
            or audit_csr or admin_csr:
        print("      The CSRs for KRA certificates have been generated in:")
    else:
        print("      No CSRs have been generated for KRA certificates.")

    if storage_csr:
        print("          storage:       %s" % storage_csr)
    if transport_csr:
        print("          transport:     %s" % transport_csr)
    if subsystem_csr:
        print("          subsystem:     %s" % subsystem_csr)
    if sslserver_csr:
        print("          SSL server:    %s" % sslserver_csr)
    if audit_csr:
        print("          audit signing: %s" % audit_csr)
    if admin_csr:
        print("          admin:         %s" % admin_csr)

    print(log.PKI_RUN_INSTALLATION_STEP_TWO)
    print(log.PKI_SPAWN_INFORMATION_FOOTER)


def print_ocsp_step_one_information(mdict):

    print(log.PKI_SPAWN_INFORMATION_HEADER)
    print("      The %s subsystem of the '%s' instance is still incomplete." %
          (deployer.subsystem_name, mdict['pki_instance_name']))
    print()
    print("      NSS database: %s" % mdict['pki_server_database_path'])
    print()

    signing_csr = mdict['pki_ocsp_signing_csr_path']
    subsystem_csr = mdict['pki_subsystem_csr_path']
    sslserver_csr = mdict['pki_sslserver_csr_path']
    audit_csr = mdict['pki_audit_signing_csr_path']
    admin_csr = mdict['pki_admin_csr_path']

    if signing_csr or subsystem_csr or sslserver_csr or audit_csr or admin_csr:
        print("      The CSRs for OCSP certificates have been generated in:")
    else:
        print("      No CSRs have been generated for OCSP certificates.")

    if signing_csr:
        print("          OCSP signing:  %s" % signing_csr)
    if subsystem_csr:
        print("          subsystem:     %s" % subsystem_csr)
    if sslserver_csr:
        print("          SSL server:    %s" % sslserver_csr)
    if audit_csr:
        print("          audit signing: %s" % audit_csr)
    if admin_csr:
        print("          admin:         %s" % admin_csr)

    print(log.PKI_RUN_INSTALLATION_STEP_TWO)
    print(log.PKI_SPAWN_INFORMATION_FOOTER)


def print_skip_configuration_information(mdict):

    print(log.PKI_SPAWN_INFORMATION_HEADER)
    print("      The %s subsystem of the '%s' instance\n"
          "      must still be configured!" %
          (deployer.subsystem_name, mdict['pki_instance_name']))
    print(log.PKI_CHECK_STATUS_MESSAGE % mdict['pki_instance_name'])
    print(log.PKI_INSTANCE_RESTART_MESSAGE % mdict['pki_instance_name'])

    print(log.PKI_ACCESS_URL % (mdict['pki_hostname'],
                                mdict['pki_https_port'],
                                deployer.subsystem_name.lower()))
    if not config.str2bool(mdict['pki_enable_on_system_boot']):
        print(log.PKI_SYSTEM_BOOT_STATUS_MESSAGE % "disabled")
    else:
        print(log.PKI_SYSTEM_BOOT_STATUS_MESSAGE % "enabled")
    print(log.PKI_SPAWN_INFORMATION_FOOTER)


def print_final_install_information(mdict):

    print(log.PKI_SPAWN_INFORMATION_HEADER)
    print("      Administrator's username:             %s" %
          mdict['pki_admin_uid'])

    if os.path.isfile(mdict['pki_client_admin_cert_p12']):
        print("      Administrator's PKCS #12 file:\n            %s" %
              mdict['pki_client_admin_cert_p12'])

    if not config.str2bool(mdict['pki_client_database_purge']) and \
            not config.str2bool(mdict['pki_clone']):
        print()
        print("      Administrator's certificate nickname:\n            %s"
              % mdict['pki_admin_nickname'])
        print("      Administrator's certificate database:\n            %s"
              % mdict['pki_client_database_dir'])

    if config.str2bool(mdict['pki_clone']):
        print()
        print("      This %s subsystem of the '%s' instance\n"
              "      is a clone." %
              (deployer.subsystem_name, mdict['pki_instance_name']))

    if mdict['pki_fips_mode_enabled']:
        print()
        print("      This %s subsystem of the '%s' instance\n"
              "      has FIPS mode enabled on this operating system." %
              (deployer.subsystem_name, mdict['pki_instance_name']))
        print()
        print("      REMINDER:  Don't forget to update the appropriate FIPS\n"
              "                 algorithms in server.xml in the '%s' instance."
              % mdict['pki_instance_name'])

    print(log.PKI_CHECK_STATUS_MESSAGE % mdict['pki_instance_name'])
    print(log.PKI_INSTANCE_RESTART_MESSAGE % mdict['pki_instance_name'])

    print(log.PKI_ACCESS_URL % (mdict['pki_hostname'],
                                mdict['pki_https_port'],
                                deployer.subsystem_name.lower()))
    if not config.str2bool(mdict['pki_enable_on_system_boot']):
        print(log.PKI_SYSTEM_BOOT_STATUS_MESSAGE % "disabled")
    else:
        print(log.PKI_SYSTEM_BOOT_STATUS_MESSAGE % "enabled")

    print(log.PKI_SPAWN_INFORMATION_FOOTER)


def log_error_details():
    e_type, e_value, e_stacktrace = sys.exc_info()
    stacktrace_list = traceback.format_list(traceback.extract_tb(e_stacktrace))
    e_stacktrace = "%s: %s\n" % (e_type.__name__, e_value)
    for l in stacktrace_list:
        e_stacktrace += l
    config.pki_log.error(e_stacktrace, extra=config.PKI_INDENTATION_LEVEL_0)
    del e_type, e_value, e_stacktrace


# PKI Deployment Entry Point
if __name__ == "__main__":
    signal.signal(signal.SIGINT, interrupt_handler)
    main(sys.argv)
