import os
import logging.config
import getpass

import click
import subprocess
import arrow
import requests
from crontab import CronTab
from dotenv import load_dotenv

from .secrets import API_GET_URL, API_POST_URL, ENVIRONMENT, BACKUPS_LOCATION, ENV_FILE_LOCATION, VENV_PATH
from .utils.utils import remove_old_backups, send_mail

logging.config.fileConfig('src/logging.conf')
logger = logging.getLogger(__name__)


def backup(app):
    # loads the .env file into memory to have access to the db credentials
    load_dotenv(ENV_FILE_LOCATION.format(app))

    backup_folder = BACKUPS_LOCATION.format(app)
    remove_old_backups(backup_folder)

    logger.info('#### Backup process for {} started ####'.format(app))
    logger.info('Attempting to make database dump')

    # attempt to run the mysqldump process and save the gzipped dump to the backups location
    ret = subprocess.run(
        'mysqldump -u{} -p{} {} | gzip > {}/{}.sql.gz'.format(
            os.environ.get('DB_USER'),
            os.environ.get('DB_PASS'),
            os.environ.get('DB_NAME'),
            backup_folder,
            arrow.now('Europe/Amsterdam').format('YYYYMMDDHHmmss')
        ),
        shell=True, stderr=True)

    # in case of error  (return code is different than 0),
    # send an email alerting of this
    # otherwise, post to gem that the backup has been done successfuly
    if ret.returncode != 0:
        logger.error('Dump failed. Reason: {}'.format(ret.stderr))
        send_mail(ret.stderr)
    else:
        logger.info('Dump completed successfully')
        response = requests.post(API_POST_URL, json={
            'secret': '0xCAFEBABE',
            'executed': arrow.now('Europe/Amsterdam').timestamp,
            'name': app
        })

        if response.status_code != 200:
            send_mail(response.text)
    logger.info('#### Backup process for {} ended ####'.format(app))


def cron():
    logger.info('#### Cron started')
    response = requests.get(API_GET_URL.format(ENVIRONMENT)).json()

    # create cron object based on user's crontab
    cron_obj = CronTab(user=getpass.getuser())

    for item in response:
        name = item['name']
        frequency = item['frequency']

        allowed_frequencies = ['daily', 'weekly']

        # in case there's bogus frequency coming from the api, silently skip the interation and move on
        if frequency not in allowed_frequencies:
            continue

        cron_command = "/bin/bash -c 'source {0}/bin/activate && {0}/bin/backopper --action=backup --app={1}'".format(VENV_PATH, name)
        freq = ''

        if frequency in allowed_frequencies:
            # becomes something like "@daily" or "@yearly" or whatever
            freq = '@{}'.format(frequency)

        # magic to compare between old cron job for this app and the new one coming composed with whatever's coming
        # from the API.

        # the reason i chose to sorta compose a new cron job is because the lib i'm using, python-crontab, does not
        # allow for easy comparison of jobs. so on each iteration of this loop, i compose a temporary cron command
        # and compare it with whatever's in the crontab on this tag
        temp_cron_command = '{} {} # {}'.format(freq, cron_command, name)

        existing_job = list(cron_obj.find_comment(name))

        # safe to assume that each tag (app) will have only one entry in the crontab
        # since find_comment returns a generator (which is translated to a list), i select manually
        # the first element in the list if the size is bigger than 0 (meaning if there's actually any elements)
        if len(existing_job) != 0:
            existing_job = existing_job[0]

        logger.info('Attempting to create cronjob for {}'.format(name))
        # if existing job is different than the one coming from the API, then update it
        # but first remove the existing rule
        if str(existing_job) != temp_cron_command:

            # this if exists only in the case when there's no rule set yet
            # then existing_job would be an empty list and stuff will mess up
            if len(existing_job) != 0:
                cron_obj.remove(existing_job)

            # creates a new cron and sets the frequency to whatever came from the API
            job = cron_obj.new(command=cron_command, comment=name)
            job.setall(freq)

            logger.info('Created cronjob for {} with command: {}'.format(name, cron_command))

            job.enable()
        else:
            logger.info('Cron already exists for {}'.format(name))

    # write all the changes (if any at all)
    cron_obj.write()
    logger.info('#### Cron ended')


@click.command()
@click.option('--action')
@click.option('--app')
def main(action, app):
    if action == 'backup':
        backup(app)
    elif action == 'cron':
        cron()
