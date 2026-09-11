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
    manage.py make_user --list                                # list all users
    manage.py make_user bob   --delete                        # remove a user

If --password is omitted for a NEW user you'll be prompted (hidden input).
"""
from getpass import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Manage login users (create/update/list/delete; admin = staff)."

    def add_arguments(self, parser):
        parser.add_argument("username", nargs="?", default=None)
        parser.add_argument("--password", default=None)
        parser.add_argument("--admin", dest="admin", action="store_true",
                            help="make (or keep) this user an admin (staff)")
        parser.add_argument("--no-admin", dest="admin", action="store_false",
                            help="make this user view-only")
        parser.add_argument("--list", action="store_true", help="list all users")
        parser.add_argument("--delete", action="store_true",
                            help="delete the named user")
        parser.set_defaults(admin=None)

    def handle(self, *args, **opts):
        User = get_user_model()

        if opts["list"]:
            users = User.objects.order_by("-is_staff", "username")
            if not users:
                self.stdout.write("no users yet")
                return
            self.stdout.write(f"{'USERNAME':24} {'ROLE':10} ACTIVE  LAST LOGIN")
            for u in users:
                role = "admin" if u.is_staff else "viewer"
                last = u.last_login.strftime("%Y-%m-%d %H:%M") if u.last_login else "never"
                self.stdout.write(f"{u.username:24} {role:10} "
                                  f"{'yes' if u.is_active else 'no ':6} {last}")
            return

        username = opts["username"]
        if not username:
            raise CommandError("username required (or use --list)")

        if opts["delete"]:
            n, _ = User.objects.filter(username=username).delete()
            if n:
                self.stdout.write(self.style.SUCCESS(f"deleted user '{username}'"))
            else:
                self.stdout.write(self.style.WARNING(f"no such user '{username}'"))
            return
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
