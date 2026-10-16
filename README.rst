task
====

Simple personal task manager, shared by humans and bots.

Installation
------------

.. code-block:: console

    $ uv tool install 'task @ git+https://github.com/TheKevJames/task@v7.3.0'

Usage
-----

Tasks are stored as files under ``$TASK_FOLDER``, one directory per namespace.

.. code-block:: console

    $ export TASK_FOLDER=~/tasks
    $ task --help
