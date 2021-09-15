import os
import sys
import uuid

from leapp.cli.commands.upgrade import util, breadcrumbs
from leapp.config import get_config
from leapp.exceptions import CommandError, LeappError
from leapp.logger import configure_logger
from leapp.utils.audit import Execution
from leapp.utils.clicmd import command, command_opt
from leapp.utils.output import (beautify_actor_exception, report_errors, report_info, report_inhibitors)

# If you are adding new parameters please ensure that they are set in the upgrade function invocation in `rerun`
# otherwise there might be errors.


@command('upgrade', help='Upgrade the current system to the next available major version.')
@command_opt('resume', is_flag=True, help='Continue the last execution after it was stopped (e.g. after reboot)')
@command_opt('reboot', is_flag=True, help='Automatically performs reboot when requested.')
@command_opt('whitelist-experimental', action='append', metavar='ActorName', help='Enable experimental actors')
@command_opt('debug', is_flag=True, help='Enable debug mode', inherit=False)
@command_opt('verbose', is_flag=True, help='Enable verbose logging', inherit=False)
@command_opt('no-rhsm', is_flag=True, help='Use only custom repositories and skip actions'
                                           ' with Red Hat Subscription Manager')
@command_opt('enablerepo', action='append', metavar='<repoid>',
             help='Enable specified repository. Can be used multiple times.')
@command_opt('channel',
             help='Set preferred channel for the IPU target.',
             choices=['ga', 'tuv', 'e4s', 'eus', 'aus'],
             value_type=str.lower)  # This allows the choices to be case insensitive
@breadcrumbs.produces_breadcrumbs
def upgrade(args, breadcrumbs):
    skip_phases_until = None
    context = str(uuid.uuid4())
    cfg = get_config()
    util.handle_output_level(args)
    configuration = util.prepare_configuration(args)
    answerfile_path = cfg.get('report', 'answerfile')
    userchoices_path = cfg.get('report', 'userchoices')

    # Processing of parameters passed by the rerun call, these aren't actually command line arguments
    # therefore we have to assume that they aren't even in `args` as they are added only by rerun.
    only_with_tags = args.only_with_tags if 'only_with_tags' in args else None
    resume_context = args.resume_context if 'resume_context' in args else None

    if os.getuid():
        raise CommandError('This command has to be run under the root user.')

    if args.resume:
        context, configuration = util.fetch_last_upgrade_context(resume_context)
        if not context:
            raise CommandError('No previous upgrade run to continue, remove `--resume` from leapp invocation to'
                               ' start a new upgrade flow')
        os.environ['LEAPP_DEBUG'] = '1' if util.check_env_and_conf('LEAPP_DEBUG', 'debug', configuration) else '0'

        if os.environ['LEAPP_DEBUG'] == '1' or util.check_env_and_conf('LEAPP_VERBOSE', 'verbose', configuration):
            os.environ['LEAPP_VERBOSE'] = '1'
        else:
            os.environ['LEAPP_VERBOSE'] = '0'
        util.restore_leapp_env_vars(context)
        skip_phases_until = util.get_last_phase(context)
    else:
        e = Execution(context=context, kind='upgrade', configuration=configuration)
        e.store()
        util.archive_logfiles()

    logger = configure_logger('leapp-upgrade.log')
    os.environ['LEAPP_EXECUTION_ID'] = context

    if args.resume:
        logger.info("Resuming execution after phase: %s", skip_phases_until)
    try:
        repositories = util.load_repositories()
    except LeappError as exc:
        raise CommandError(exc.message)
    workflow = repositories.lookup_workflow('IPUWorkflow')(auto_reboot=args.reboot)
    util.process_whitelist_experimental(repositories, workflow, configuration, logger)
    util.warn_if_unsupported(configuration)
    with beautify_actor_exception():
        logger.info("Using answerfile at %s", answerfile_path)
        workflow.load_answers(answerfile_path, userchoices_path)

        # Set the locale, so that the actors parsing command outputs that might be localized will not fail
        os.environ['LC_ALL'] = 'en_US.UTF-8'
        os.environ['LANG'] = 'en_US.UTF-8'
        workflow.run(context=context, skip_phases_until=skip_phases_until, skip_dialogs=True,
                     only_with_tags=only_with_tags)

    logger.info("Answerfile will be created at %s", answerfile_path)
    workflow.save_answers(answerfile_path, userchoices_path)
    report_errors(workflow.errors)
    report_inhibitors(context)
    util.generate_report_files(context)
    report_files = util.get_cfg_files('report', cfg)
    log_files = util.get_cfg_files('logs', cfg)
    report_info(report_files, log_files, answerfile_path, fail=workflow.failure)

    if workflow.failure:
        sys.exit(1)


def register(base_command):
    """
        Registers `leapp upgrade`
    """
    base_command.add_sub(upgrade)
