task
====

Simple personal task manager, shared by an operator and agents.

Installation
------------

.. code-block:: console

    $ uv tool install 'task @ git+https://github.com/TheKevJames/task@v8.0.0'

Usage
-----

Tasks are stored as files under ``$TASK_FOLDER``, one directory per namespace.

.. code-block:: console

    $ export TASK_FOLDER=~/tasks
    $ task --help
