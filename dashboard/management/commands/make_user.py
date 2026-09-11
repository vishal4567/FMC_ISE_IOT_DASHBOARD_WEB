"""
Create or update a dashboard login user.

  Roles:
    --admin   staff user - can change configuration (Config / Settings pages)
    (default) view-only user - can see everything, change nothing

    manage.py make_user alice --password 'S3cret!!' --admin
    manage.py make_user bob   --password 'View3r!!'
    manage.py make_user alice --password 'NewPass99'          # reset password
    manage.py make_user alice --admin                         # promote to admin
    manage.py make_user bob   --no-admin                      # demote to viewer

If --password is omitted for a NEW user you'll be prompted (hidden input).
"""
from getpass import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create/update a login user (admin = staff, else view-only)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--password", default=None)
        parser.add_argument("--admin", dest="admin", action="store_true",
                            help="make (or keep) this user an admin (staff)")
        parser.add_argument("--no-admin", dest="admin", action="store_false",
                            help="make this user view-only")
        parser.set_defaults(admin=None)

    def handle(self, *args, **opts):
        User = get_user_model()
        username = opts["username"]
        user = User.objects.filter(username=username).first()
        creating = user is None

        password = opts["password"]
        if creating and not password:
            password = getpass("Password: ")
            if password != getpass("Confirm password: "):
                raise CommandError("passwords do not match")

        if creating:
            user = User(username=username)
        if password:
            user.set_password(password)
        if opts["admin"] is not None:
            user.is_staff = opts["admin"]
        elif creating:
            user.is_staff = False
        user.is_active = True
        user.save()

        role = "ADMIN (staff)" if user.is_staff else "view-only"
        self.stdout.write(self.style.SUCCESS(
            f"{'created' if creating else 'updated'} user '{username}' - {role}"))
