from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from .models import Contact, Workspace

User = get_user_model()


class LoginForm(forms.Form):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True, "placeholder": "you@company.com"}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "placeholder": "Your password"}))
    remember = forms.BooleanField(required=False, initial=True, label="Keep me signed in")


class SignupForm(forms.Form):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists. Log in instead.")
        return email

    def clean_password(self):
        pw = self.cleaned_data["password"]
        validate_password(pw)
        return pw


class WorkspaceCreateForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ["name", "industry", "department", "size"]
        labels = {"name": "Company / workspace name"}


class WorkspaceInfoForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ["name", "description", "industry", "department", "size", "slug", "logo", "country", "address1",
                  "address2", "city", "zip_code", "phone", "email", "website"]
        labels = {"name": "Company name", "slug": "Workspace domain name", "address1": "Address line 1",
                  "address2": "Address line 2", "zip_code": "ZIP code", "email": "Email address", "website": "Website URL"}
        widgets = {"description": forms.Textarea(attrs={"rows": 3, "placeholder": "Describe your company"})}


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["name", "email", "phone", "company", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class ProfileForm(forms.Form):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150, required=False)
    username = forms.CharField(max_length=150)
    phone = forms.CharField(max_length=40, required=False)
    avatar = forms.ImageField(required=False)
    position = forms.CharField(max_length=120, required=False)
    fax = forms.CharField(max_length=40, required=False)
    video_link = forms.URLField(required=False)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_username(self):
        u = self.cleaned_data["username"]
        if User.objects.filter(username__iexact=u).exclude(pk=self.user.pk).exists():
            raise forms.ValidationError("This username is taken.")
        return u
