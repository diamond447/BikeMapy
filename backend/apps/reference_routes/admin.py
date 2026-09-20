from django.contrib import admin

from .models import (
    ReferenceCollection,
    ReferenceImport,
    ReferenceRecomputation,
    ReferenceRoute,
    ReferenceRouteVersion,
)

admin.site.register(ReferenceCollection)
admin.site.register(ReferenceImport)
admin.site.register(ReferenceRoute)
admin.site.register(ReferenceRouteVersion)
admin.site.register(ReferenceRecomputation)
