#!/usr/bin/env python
# -*- coding: utf-8 -*-


# =============================================================================
# IMPORTS
# =============================================================================

import sys
import os

from django.core.management.commands import startproject

import six

import otree


# =============================================================================
# COMMAND
# =============================================================================

class Command(startproject.Command):
    help = ("Creates a new oTree project.")

    def render_runtime(self, options):
        project_name, target = options['name'], options['directory']
        if target is None:
            top_dir = os.path.join(os.getcwd(), project_name)
        else:
            top_dir = os.path.abspath(os.path.expanduser(target))

        runtime_path = os.path.join(top_dir, "runtime.txt")
        version = ".".join(map(str, sys.version_info[:3])) + "\n"

        with open(runtime_path, "w") as fp:
            fp.write(version)

    def handle(self, *args, **options):
        answer = None
        while not answer or answer not in "yn":
            answer = six.moves.input("Include sample games? (y or n): ")
            if not answer:
                answer = "y"
                break
            else:
                answer = answer[0].lower()

        if answer == "y":
            location = "https://github.com/oTree-org/oTree/archive/master.zip"
        else:
            location = os.path.join(
                os.path.dirname(otree.__file__), 'project_template')
        if options.get('template', None) is None:
            options['template'] = location
        super(Command, self).handle(*args, **options)

        self.render_runtime(options)
