#!/usr/bin/env python
"""
CANVAS FILE SYNC v1.1
    Syncs all Canvas files from specified courses to a local folder,
    then uploads them to cloud storage using rclone.
by Jensen Hwa
since October 2018
"""
import argparse
import datetime
import filecmp
import os
import shutil
import subprocess
import tempfile
from enum import Enum
from pathlib import Path

import pytz
import requests
import yaml

s = requests.Session()


def getfile_insensitive(path):
    directory, filename = os.path.split(path)
    directory, filename = (directory or '.'), filename.lower()
    for f in os.listdir(directory):
        newpath = os.path.join(directory, f)
        if f.lower() == filename:
            return newpath


def isfile_insensitive(path):
    return getfile_insensitive(path) is not None


def add_before_ext(file_name, end) -> str:
    temp_name = file_name
    pos = temp_name.rfind('.')
    if pos == -1:
        temp_name += end
    else:
        temp_name = temp_name[:pos] + end + temp_name[pos:]
    return temp_name


def download(file, dest, request_headers):
    if(file['url'] == ''):
        print(f"Warning: {file['display_name']} ignored due to canvas error")
        return
    if file['url'].startswith('URL:'):
        with open(dest, 'w') as fd:
            fd.write(f"[InternetShortcut]\nURL={file['url'][4:]}\n")
    elif file['url'].startswith('SubHeader:'):
        Path(dest).touch()
    else:
        r = s.get(file['url'], headers=request_headers, stream=True)
        if r.status_code == 200:
            with open(dest, 'wb') as fd:
                for chunk in r.iter_content(chunk_size=128):
                    fd.write(chunk)
            os.utime(dest, (file["_time"].timestamp(), file["_time"].timestamp()))
        else:
            raise ConnectionError('Non-200 status code', file['display_name'])


def do_all_pages(req_url, headers, method_to_run, *const_args):
    while req_url != '':
        # Downloading files in the respective folders
        response = s.get(req_url, headers=headers)
        response.raise_for_status()
        try:
            req_url = response.links['next']['url']
        except KeyError:
            req_url = ''
        for thing in response.json():
            method_to_run(thing, *const_args)


def recursive_old_dir_move(root_src_dir, root_dst_dir):
    for src_dir, dirs, files in os.walk(root_src_dir):
        dst_dir = src_dir.replace(root_src_dir, root_dst_dir, 1)
        os.makedirs(dst_dir, exist_ok=True)
        for file_ in files:
            src_file = os.path.join(src_dir, file_)
            dst_file = os.path.join(dst_dir, file_)
            if os.path.exists(dst_file):
                # in case of the src and dst are the same file
                if filecmp.cmp(src_file, dst_file):
                    continue
                os.rename(dst_file, add_before_ext(dst_file,
                                                   ' v' + datetime.datetime.fromtimestamp(os.path.getmtime(dst_file))
                                                   .astimezone(local_timezone).strftime(time_fmt)))
            shutil.move(src_file, dst_dir)
    shutil.rmtree(root_src_dir)


class Course:
    class Type(Enum):
        FILES = 1
        MODULES = 2

    def __init__(self, cconfig):
        global update_config
        self.course_id = str(cconfig["id"])
        if "modules" in cconfig:
            self.type = Course.Type.MODULES
        else:
            self.type = Course.Type.FILES
        self.access_token = config["tokens"][cconfig["access_token"]]
        # self.rclone = cconfig["rclone"]
        self.headers = {'Authorization': 'Bearer ' + self.access_token}
        r_course = s.get(base_url + 'courses/' + self.course_id, headers=self.headers)
        r_course.raise_for_status()
        self.course_dict = r_course.json()
        self.folder_dict = {}
        self.file_set = set()
        self.skipped, self.updated, self.downloaded, self.errors = 0, 0, 0, 0
        if args.verbosity >= 1:
            print(self.course_dict['name'])
        if "name" not in cconfig:
            update_config = True
            cconfig["name"] = self.course_dict['name'].replace(":", "-")
            if self.type == Course.Type.MODULES:
                cconfig["name"] += " Modules"
        self.course_dir = os.path.join(base_dir, cconfig["name"])
        if not os.path.isdir(self.course_dir):
            if args.verbosity >= 1:
                print("  Creating ", self.course_dir)
            os.makedirs(self.course_dir)

    def sync_local(self):
        if args.verbosity >= 1:
            print("  Syncing")
        if self.type == Course.Type.MODULES:
            modules_request_url = f"{base_url}courses/{self.course_id}/modules"
            do_all_pages(modules_request_url, self.headers, self._parse_module)
        else:
            folders_request_url = base_url + 'courses/' + self.course_id + '/folders?per_page=999999'
            do_all_pages(folders_request_url, self.headers, self._parse_folder)
            files_request_url = base_url + 'courses/' + self.course_id + '/files?per_page=999999'
            do_all_pages(files_request_url, self.headers, self._parse_file)

    def onto_local(self):
        for root, dirnames, filenames in os.walk(self.course_dir):
            # Skip if root contains .old or deleted since os.walk was called
            if ".old" in os.path.normpath(root).split(os.path.sep) or not os.path.exists(root):
                continue
            for filename in filenames:
                if os.path.join(root, filename) not in self.file_set:
                    if args.verbosity >= 1:
                        print(" ", filename, "file no longer in Canvas, deleting")
                    # os.makedirs(os.path.join(root, ".old"), exist_ok=True)
                    # shutil.move(os.path.join(root, filename), os.path.join(root, ".old", filename))
                    os.remove(os.path.join(root, filename))
            for dirname in dirnames:
                if dirname == ".old":
                    continue
                if os.path.join(root, dirname) not in self.folder_dict.values():
                    if args.verbosity >= 1:
                        print(" ", dirname, "folder no longer in Canvas, deleteing")
                    # os.makedirs(os.path.join(root, ".old"), exist_ok=True)
                    # os.removedirs(os.path.join(root, dirname))
                    shutil.rmtree(os.path.join(root, dirname))

                    # recursive_old_dir_move(os.path.join(root, dirname), os.path.join(root, ".old", dirname))

    def sync_cloud(self):
        for dest in self.rclone:
            if args.verbosity >= 1:
                print("  Uploading to", dest["drive"])
            proc_args = ["rclone", "sync", self.course_dir, dest["drive"] + ":" + dest["path"]]
            if args.dryrun:
                proc_args.append("-n")
            sync_proc = subprocess.run(proc_args, capture_output=True, text=True)
            if sync_proc.stderr:
                print("Failed to sync", self.course_dict['name'], "to", dest["drive"])
                print(sync_proc.stderr)

    def _parse_module(self, module):
        folder_name = module['name']
        folder_dir = os.path.join(self.course_dir, folder_name)
        self.folder_dict[module['id']] = folder_dir
        if not os.path.isdir(folder_dir):
            if args.verbosity >= 1:
                print('  Creating', folder_dir)
            os.makedirs(folder_dir)

        items_request_url = f"{base_url}courses/{self.course_id}/modules/{module['id']}/items"
        do_all_pages(items_request_url, self.headers, self._parse_moduleitem, module['id'])

    def _parse_moduleitem(self, moduleitem, moduleid):
        if moduleitem['type'] == 'File':
            r = s.get(moduleitem['url'], headers=self.headers)
            r.raise_for_status()
            file = r.json()
        elif moduleitem['type'] == 'SubHeader':
            file = {'url': f"SubHeader:{moduleitem['title']}",
                    'display_name': moduleitem['title'],
                    'modified_at': datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    'updated_at': datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        else:
            file = {'url': f"URL:{moduleitem['html_url']}",
                    'display_name': f"{moduleitem['title']}.url",
                    'modified_at': datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    'updated_at': datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        file['display_name'] = f"{moduleitem['position']}{moduleitem['indent']*'~'} {file['display_name']}"
        file['folder_id'] = moduleid
        self._parse_file(file)

    def _parse_folder(self, folder):
        # Create subfolders
        # Creating a dict to store folder name as file object has only folder ID
        folder_name = str(folder['full_name'])[13:]
        folder_dir = os.path.join(self.course_dir, folder_name)
        self.folder_dict[folder['id']] = folder_dir
        if not os.path.isdir(folder_dir):
            if args.verbosity >= 1:
                print('  Creating', folder_dir)
            os.makedirs(folder_dir)

    def _parse_file(self, file):
        # Decide file location
        file_location = self.folder_dict[file['folder_id']]
        file_url = file['url']
        file_name = file['display_name'].replace('/', '-')
        file_path = os.path.join(file_location, file_name)
        file_utc = file['_time'] = datetime.datetime.strptime(file['modified_at'], "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=datetime.timezone.utc)
        # Detect if different file with same name, case-insensitive, exists
        if not os.path.isfile(file_path) and isfile_insensitive(file_path):
            # Append 'c' and updated time to filename so it can be uploaded to cloud
            file_name = add_before_ext(file_name, ' c' + file_utc.astimezone(local_timezone).strftime(time_fmt))
            file_path = os.path.join(file_location, file_name)
            if args.verbosity >= 1:
                print('  Downloading new case', file_name)
            try:
                download(file, file_path, self.headers)
                self.downloaded += 1
            except ConnectionError as err:
                print(err.args)
                self.errors += 1
                return
        # Look for newer version (updated on Canvas at a later time than local copy)
        elif os.path.isfile(file_path) and os.path.getmtime(file_path) < file_utc.timestamp():
            tf = tempfile.NamedTemporaryFile(delete=False)
            temp_file_path = tf.name
            try:
                download(file, temp_file_path, self.headers)
            except ConnectionError as err:
                print(err.args)
                self.errors += 1
                return
            tf.close()
            if not filecmp.cmp(file_path, temp_file_path):
                if args.verbosity >= 1:
                    print('  Found newer version of', file['display_name'], 'updated at', file['updated_at'])
                self.updated += 1
                # new_file_name = add_before_ext(file_name,
                #                                ' v' + datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                #                                .astimezone(local_timezone).strftime(time_fmt))
                # os.makedirs(os.path.join(file_location, ".old"), exist_ok=True)
                # shutil.move(file_path, os.path.join(file_location, ".old", new_file_name))
                os.remove(file_path)
                shutil.copy2(temp_file_path, file_path)
            else:
                self.skipped += 1
            os.remove(temp_file_path)
        elif not os.path.isfile(file_path) and file_url != '':
            if args.verbosity >= 1:
                print('  Downloading new', file_name)
            try:
                download(file, file_path, self.headers)
                self.downloaded += 1
            except ConnectionError as err:
                print(err.args)
                self.errors += 1
                return
        else:
            if args.verbosity >= 2:
                print('  Skipped', file_name, 'because latest version already exists')
            self.skipped += 1
        self.file_set.add(file_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Syncs all Canvas files from specified courses to a local folder,\
                                                  then uploads them to cloud storage using rclone.")
    parser.add_argument("-v", "--verbosity", help="increase output verbosity", action="count", default=1)
    parser.add_argument("-n", "--dryrun", help="have rclone do a trial run with no permanent changes",
                        action="store_true")
    args = parser.parse_args()
    statsbycourse = []
    base_dir = os.path.dirname(__file__)
    update_config = False
    with open(os.path.join(base_dir, "settings.yaml"), "r+", encoding='utf-8') as cf:
        config = yaml.safe_load(cf)
        local_timezone = pytz.timezone(config["timezone"])
        base_url = config["base_url"]
        time_fmt = config["time_fmt"]
        for c in config["courses"]:
            course = Course(c)
            course.sync_local()
            course.onto_local()
            # course.sync_cloud()
            statsbycourse.append(f"  {course.course_dict['name']}: "
                                 f"{course.downloaded} new, "
                                 f"{course.updated} updated, "
                                 f"{course.skipped} skipped, "
                                 f"{course.errors} errors\n")
        if args.verbosity >= 1:
            print('SUMMARY:')
            for line in statsbycourse:
                print(line)
        if update_config:
            cf.seek(0)
            yaml.dump(config, cf, encoding="utf-8")
            cf.truncate()
