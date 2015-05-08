from flask import Blueprint, make_response, request, render_template, \
    url_for, send_from_directory, session as flask_session, redirect, flash
import json
import os
from flask_security.decorators import login_required, roles_required
from flask_security.core import current_user
from transcriber.app_config import UPLOAD_FOLDER
from werkzeug import secure_filename
from transcriber.models import FormMeta, FormSection, FormField, \
    Image, TaskGroup, User
from transcriber.database import db
from transcriber.helpers import slugify, add_images, pretty_transcriptions
from flask_wtf import Form
from transcriber.dynamic_form import NullableIntegerField as IntegerField, \
    NullableDateTimeField as DateTimeField, \
    NullableDateField as DateField
from transcriber.dynamic_form import validate_blank_not_legible
from wtforms.fields import BooleanField, StringField
from wtforms.validators import DataRequired
from datetime import datetime, timedelta
from transcriber.app_config import TIME_ZONE
from sqlalchemy import Table, Column, MetaData, String, Boolean, \
        Integer, DateTime, Date, text, and_, or_
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.orm import aliased
from uuid import uuid4
from operator import attrgetter, itemgetter
from itertools import groupby
from io import StringIO
import pytz
import ast
from documentcloud import DocumentCloud
from .app_config import DOCUMENTCLOUD_USER, DOCUMENTCLOUD_PW

views = Blueprint('views', __name__)

ALLOWED_EXTENSIONS = set(['pdf', 'png', 'jpg', 'jpeg'])

DATA_TYPE = {
    'boolean': Boolean,
    'string': String,
    'integer': Integer,
    'datetime': DateTime,
    'date': Date
}

SQL_DATA_TYPE = {
    'boolean': 'BOOLEAN',
    'string': 'VARCHAR',
    'integer': 'INTEGER',
    'datetime': 'TIMESTAMP WITH TIME ZONE',
    'date': 'DATE'
}

FORM_TYPE = {
    'boolean': BooleanField,
    'string': StringField,
    'integer': IntegerField,
    'datetime': DateTimeField,
    'date': DateField
}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@views.route('/')
def index():
    tasks = db.session.query(FormMeta)\
            .filter(or_(FormMeta.status != 'deleted', 
                        FormMeta.status == None))\
            .order_by(FormMeta.task_group_id, FormMeta.index)\
            .all()
            # order by due date here
    t = []
    groups = []
    for task in tasks:
        # make the progress bar depend on reviews (#docs * #reviewers) instead of documents?
        task_dict = task.as_dict()
        reviewer_count = task_dict['reviewer_count']
        task_id = task_dict['id']
        if reviewer_count == None: # clean this up
            reviewer_count = 1

        docs_left = db.session.query(Image)\
                .filter(Image.form_id == task_id)\
                .filter(Image.view_count < reviewer_count)\
                .count()
        docs_total = db.session.query(Image)\
                .filter(Image.form_id == task.id)\
                .count()
        docs_complete = docs_total - docs_left
        reviews_complete = 0
        for i in range(1, reviewer_count+1):
            n = db.session.query(Image)\
                .filter(Image.form_id == task_id)\
                .filter(Image.view_count == i).count()
            reviews_complete+=n*i

        if docs_total > 0 and reviewer_count > 0:
            doc_percent = int(float(docs_complete)/float(docs_total)*100)
            review_percent = int(float(reviews_complete)/float(reviewer_count*docs_total)*100)
        else:
            doc_percent = None
            review_percent = None

        if task.task_group_id not in groups and review_percent < 100:
            is_top_task = True
            groups.append(task.task_group_id)
        else:
            is_top_task = False

        progress_dict = {}
        progress_dict['docs_percent'] = doc_percent
        progress_dict['docs_complete'] = docs_complete
        progress_dict['docs_total'] = docs_total 
        progress_dict['reviews_complete'] = reviews_complete
        progress_dict['reviews_total'] = reviewer_count*docs_total
        progress_dict['review_percent'] = review_percent
        t.append([task, progress_dict, is_top_task])
        
    return render_template('index.html', tasks=t)

@views.route('/about/')
def about():
    return render_template('about.html')

@views.route('/upload/',methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def upload():
    image = None
    if request.method == 'POST':

        election_id = request.form.get('election_id')
        hiearchy_filter = request.form.get('hierarchy_filter')

        client = DocumentCloud(DOCUMENTCLOUD_USER, DOCUMENTCLOUD_PW)
        all_docs = client.projects.get_by_title('ndi').document_list
        doc_list = [doc for doc in all_docs if doc.data['election_id']==election_id]

        # if there is a hierarchy filter, filter docs further here
        ##########################################################

        if len(doc_list) > 0:
            first_doc = doc_list[0]
            flask_session['image'] = first_doc.pdf_url
            flask_session['image_type'] = 'pdf'
            flask_session['doc_id_list'] = [doc.id for doc in doc_list]

            return redirect(url_for('views.form_creator'))
        else:
            flash("No DocumentCloud images found")

    return render_template('upload.html', image=image)

@views.route('/delete-part/', methods=['DELETE'])
@login_required
@roles_required('admin')
def delete_part():
    part_id = request.form.get('part_id')
    part_type = request.form.get('part_type')
    r = {
        'status': 'ok',
        'message': ''
    }
    status_code = 200
    if not part_id:
        r['status'] = 'error'
        r['message'] = 'Need the ID of the component to remove'
        status_code = 400
    elif not part_type:
        r['status'] = 'error'
        r['message'] = 'Need the type of component to remove'
        status_code = 400
    else:
        thing = None
        if part_type == 'section':
            thing = FormSection
        elif part_type == 'field':
            thing = FormField
        elif part_type == 'form':
            thing = FormMeta
        if thing:
            it = db.session.query(thing).get(part_id)
            if it:
                it.status = 'deleted'
                db.session.add(it)
                db.session.commit()
            else:
                r['status'] = 'error'
                r['message'] = '"{0}" is not a valid component ID'.format(part_id)
                status_code = 400
        else:
            r['status'] = 'error'
            r['message'] = '"{0}" is not a valid component type'.format(part_type)
            status_code = 400
    if part_type == 'form':
        flash("Task deleted")
    response = make_response(json.dumps(r), status_code)
    response.headers['Content-Type'] = 'application/json'
    return response

@views.route('/form-creator/', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def form_creator():
    form_meta = FormMeta()
    if request.args.get('form_id'):
        form = db.session.query(FormMeta).get(request.args['form_id'])
        if form:
            form_meta = form
            flask_session['image'] = form.sample_image
            flask_session['image_type'] = form.sample_image.rsplit('.', 1)[1].lower()
    if not flask_session.get('image'):
        return redirect(url_for('views.upload'))
    engine = db.session.bind
    if request.method == 'POST':
        name = request.form['task_name']
        form_meta.name = name
        form_meta.description = request.form['task_description']
        form_meta.slug = slugify(name)
        form_meta.last_update = datetime.now().replace(tzinfo=TIME_ZONE)
        form_meta.sample_image = flask_session['image']
        if request.form.get('task_group_id'):
            task_group = db.session.query(TaskGroup)\
                    .get(request.form['task_group_id'])
        else:
            task_group = TaskGroup(name=request.form['task_group'],
                    description=request.form.get('task_group_description'))
        form_meta.task_group = task_group
        form_meta.deadline = request.form['deadline']
        form_meta.reviewer_count = request.form['reviewer_count']
        db.session.add(form_meta)
        db.session.commit()
        section_fields = {}
        sections = {}
        field_datatypes = {}
        changed_field_names = []
        for k,v in request.form.items():
            parts = k.split('_')
            if 'section' in parts:
                if len(parts) == 2:
                    # You've got yourself a section
                    section_idx = k.split('_')[-1]
                    section = db.session.query(FormSection)\
                            .filter(FormSection.index == section_idx)\
                            .filter(FormSection.form == form_meta)\
                            .first()
                    if not section:
                        section = FormSection(name=v, 
                                              slug=slugify(v),
                                              index=section_idx,
                                              form=form_meta)
                    else:
                        section.name = v
                        section.slug = slugify(v)
                    sections[section_idx] = section
                if len(parts) == 5:
                    # You've got yourself a field data type
                    field_idx = k.split('_')[-1]
                    section_idx = k.split('_')[2]
                    try:
                        field_datatypes[section_idx][field_idx] = v
                    except KeyError:
                        field_datatypes[section_idx] = {field_idx: v}
                if len(parts) == 4:
                    # You've got yourself a field
                    field_idx = k.split('_')[-1]
                    section_idx = k.split('_')[1]
                    field = db.session.query(FormField)\
                            .filter(FormField.index == field_idx)\
                            .filter(FormSection.index == section_idx)\
                            .filter(FormField.form == form_meta)\
                            .first()
                    if not field: # adding a new field
                        section = db.session.query(FormSection)\
                                .filter(FormSection.index == section_idx)\
                                .filter(FormSection.form == form_meta)\
                                .first()
                        field = FormField(name=v,
                                          slug=slugify(v),
                                          index=field_idx,
                                          form=form_meta,
                                          section=section)
                    else:
                        # keeping track of field names that have changed
                        if field.name != v:                        
                            changed_field_names.append([field.slug, slugify(v)])
                            field.name = v
                            field.slug = slugify(v)
                    db.session.add(field)
                    try:
                        section_fields[section_idx].append(field)
                    except KeyError:
                        section_fields[section_idx] = [field]
        for section_id, section in sections.items():
            section.fields = section_fields[section_id]
            for field in section.fields:
                # if this is a new field (all existing fields will already have a data type)
                if not field.data_type:
                    field.data_type = field_datatypes[section_id][unicode(field.index)]
                    field.section = section
                    db.session.add(field)
            db.session.add(section)
        db.session.commit()
        db.session.refresh(form_meta, ['fields', 'table_name'])
        
        metadata = MetaData()

        # if the form already exists

        if form_meta.table_name:
            
            #update column names
            for column in changed_field_names:
                old_slug = column[0]
                new_slug = column[1]
                rename = 'ALTER TABLE "{0}" RENAME COLUMN {1} TO {2}'\
                            .format(form_meta.table_name, old_slug, new_slug)
                rename_blank = 'ALTER TABLE "{0}" RENAME COLUMN {1}_blank TO {2}_blank'\
                            .format(form_meta.table_name, old_slug, new_slug)
                rename_not_legible = 'ALTER TABLE "{0}" RENAME COLUMN {1}_not_legible TO {2}_not_legible'\
                            .format(form_meta.table_name, old_slug, new_slug)
                rename_altered = 'ALTER TABLE "{0}" RENAME COLUMN {1}_altered TO {2}_altered'\
                            .format(form_meta.table_name, old_slug, new_slug)

                with engine.begin() as conn:
                    conn.execute(rename)
                    conn.execute(rename_blank)
                    conn.execute(rename_not_legible)
                    conn.execute(rename_altered)

            table = Table(form_meta.table_name, metadata, 
                          autoload=True, autoload_with=engine)
            new_columns = set([f.slug for f in form_meta.fields])
            existing_columns = set([c.name for c in table.columns])
            add_columns = new_columns - existing_columns
            for column in add_columns:
                field = [f for f in form_meta.fields if f.slug == unicode(column)][0]
                sql_type = SQL_DATA_TYPE[field.data_type]
                alt = 'ALTER TABLE "{0}" ADD COLUMN {1} {2}'\
                        .format(form_meta.table_name, field.slug, sql_type)
                blank = '''
                    ALTER TABLE "{0}" 
                    ADD COLUMN {1}_blank boolean
                    '''.format(form_meta.table_name, field.slug)
                not_legible = '''
                    ALTER TABLE "{0}" 
                    ADD COLUMN {1}_not_legible boolean
                    '''.format(form_meta.table_name, field.slug)
                altered = '''
                    ALTER TABLE "{0}" 
                    ADD COLUMN {1}_altered boolean
                    '''.format(form_meta.table_name, field.slug)
                with engine.begin() as conn:
                    conn.execute(alt)
                    conn.execute(blank)
                    conn.execute(not_legible)
                    conn.execute(altered)

        # if the form does not exist yet
        else:
            form_meta.table_name = '{0}_{1}'.format(
                    unicode(uuid4()).rsplit('-', 1)[1], 
                    form_meta.slug)[:60]
            cols = [
                Column('date_added', DateTime(timezone=True), 
                    server_default=text('CURRENT_TIMESTAMP')),
                Column('transcriber', String),
                Column('id', Integer, primary_key=True),
                Column('image_id', Integer)
            ]
            for field in form_meta.fields:
                dt = DATA_TYPE.get(field.data_type, String)
                if field.data_type  == 'datetime':
                    dt = DateTime(timezone=True)
                cols.append(Column(field.slug, dt))
                cols.append(Column('{0}_blank'.format(field.slug), Boolean))
                cols.append(Column('{0}_not_legible'.format(field.slug), Boolean))
                cols.append(Column('{0}_altered'.format(field.slug), Boolean))
            table = Table(form_meta.table_name, metadata, *cols)
            engine = db.session.bind
            table.create(bind=engine)
            db.session.add(form_meta)
            db.session.commit()
            # add document cloud images to the images table, assign to form_id
            ##################################################################
            add_images(form_meta.id)
        return redirect(url_for('views.index'))
    next_section_index = 2
    next_field_indicies = {1: 2}
    if form_meta.id:
        sel = ''' 
            SELECT 
                s.index + 1 as section_index
            FROM form_meta as m
            JOIN form_section as s
                ON m.id = s.form_id
            WHERE m.id = :form_id
            ORDER BY section_index DESC
            LIMIT 1
        '''
        next_section_index = engine.execute(text(sel), 
                                form_id=form_meta.id).first()[0]
        sel = ''' 
            SELECT 
                s.index as section_index,
                MAX(f.index) AS field_index
            FROM form_meta as m
            JOIN form_section as s
                ON m.id = s.form_id
            JOIN form_field as f
                ON s.id = f.section_id
            WHERE m.id = :form_id
            GROUP BY s.id
        '''
        next_field_indicies = {f[0]: f[1] for f in \
                engine.execute(text(sel), form_id=form_meta.id)}
    form_meta = form_meta.as_dict()
    if form_meta['sections']:
        for section in form_meta['sections']:
            section['fields'] = sorted(section['fields'], key=itemgetter('index'))
        form_meta['sections'] = sorted(form_meta['sections'], key=itemgetter('index'))
    return render_template('form-creator.html', 
                           form_meta=form_meta,
                           next_section_index=next_section_index,
                           next_field_index=next_field_indicies)

@views.route('/get-task-group/')
@login_required
@roles_required('admin')
def get_task_group():
    term = request.args.get('term')
    where = TaskGroup.name.ilike('%%%s%%' % term)
    base_query = db.session.query(TaskGroup).filter(where)
    names = [{'name': t.name, 'id': str(t.id), 'description': t.description} \
            for t in base_query.all()]
    resp = make_response(json.dumps(names))
    resp.headers['Content-Type'] = 'application/json'
    return resp

@views.route('/edit-task-group/', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def edit_task_group():
    if not request.args.get('group_id'):
        flash('Group ID is required')
        return redirect(url_for('views.index'))
    if request.method == 'POST':
        form = request.form
        if form['task_array']:
            priorities = ast.literal_eval(form['task_array'])

            save_ok = True
            for i, task_id in enumerate(priorities):
                task = db.session.query(FormMeta).get(int(task_id))
                if task:
                    task.index = i
                    db.session.add(task)
                    db.session.commit()
                else:
                    flash("Error saving priorities")
                    save_ok = False
                    break

            if save_ok:
                flash("Priorities saved")

        else:
            flash("Error saving priorities")

    task_group = db.session.query(TaskGroup).get(request.args['group_id'])
    return render_template('edit-task-group.html',task_group=task_group)

@views.route('/transcribe-intro/', methods=['GET', 'POST'])
def transcribe_intro():
    if not request.args.get('task_id'):
        return redirect(url_for('views.index'))
    task = db.session.query(FormMeta)\
            .filter(FormMeta.id == request.args['task_id'])\
            .first()
    task_dict = task.as_dict()
    return render_template('transcribe-intro.html', task=task_dict)

class DynamicForm(Form):
    pass

@views.route('/transcribe/', methods=['GET', 'POST'])
def transcribe():
    if not request.args.get('task_id'):
        return redirect(url_for('views.index'))
    engine = db.session.bind
    section_sq = db.session.query(FormSection)\
            .filter(or_(FormSection.status != 'deleted', 
                        FormSection.status == None))\
            .order_by(FormSection.index)\
            .subquery()
    field_sq = db.session.query(FormField)\
            .filter(or_(FormField.status != 'deleted', 
                        FormField.status == None))\
            .order_by(FormField.index)\
            .subquery()
    task = db.session.query(FormMeta)\
            .join(section_sq)\
            .join(field_sq)\
            .filter(FormMeta.id == request.args['task_id'])\
            .first()
    form = DynamicForm
    task_dict = task.as_dict()
    task_dict['sections'] = []
    bools = []
    for section in sorted(task.sections, key=attrgetter('index')):
        section_dict = {'name': section.name, 'fields': []}
        for field in sorted(section.fields, key=attrgetter('index')):
            message = u'If the "{0}" field is either blank or not legible, \
                    please mark the appropriate checkbox'.format(field.name)
            if field.data_type == 'boolean':
                bools.append(field.slug)
            ft = FORM_TYPE[field.data_type]()
            setattr(form, field.slug, ft)
            blank = '{0}_blank'.format(field.slug)
            not_legible = '{0}_not_legible'.format(field.slug)
            altered = '{0}_altered'.format(field.slug)
            setattr(form, blank, BooleanField())
            setattr(form, not_legible, BooleanField())
            setattr(form, altered, BooleanField())
            bools.extend([blank, not_legible, altered])
            section_dict['fields'].append(field)
        task_dict['sections'].append(section_dict)

    all_fields = set([f.slug for f in section_dict['fields']])
    for field in all_fields:
        setattr(form, 'validate_{0}'.format(field), validate_blank_not_legible)

    current_time = datetime.now().replace(tzinfo=pytz.UTC)
    expire_time = current_time+timedelta(seconds=5*60)

    # update image checkout expiration
    expired = db.session.query(Image).filter(Image.checkout_expire < current_time).all()
    if expired:
        for expired_image in expired:
            expired_image.checkout_expire = None
            db.session.add(expired_image)
            db.session.commit()
            
    if request.method == 'POST':
        form = form(request.form)
        if form.validate():

            image = db.session.query(Image).get(flask_session['image_id'])
            if not image.checkout_expire or image.checkout_expire < current_time:
                flash("Form has expired", "expired")
            else:
                if current_user.is_anonymous():
                    username = request.remote_addr
                else:
                    username = current_user.name

                ins_args = {
                    'transcriber': username,
                    'image_id': flask_session['image_id'],
                }
                for k,v in request.form.items():
                    if k != 'csrf_token':
                        if v:
                            ins_args[k] = v
                        else:
                            ins_args[k] = None
                if not set(bools).intersection(set(ins_args.keys())):
                    for f in bools:
                        ins_args[f] = False
                ins = ''' 
                    INSERT INTO "{0}" ({1}) VALUES ({2})
                '''.format(task.table_name, 
                           ','.join([f for f in ins_args.keys()]),
                           ','.join([':{0}'.format(f) for f in ins_args.keys()]))

                with engine.begin() as conn:
                    conn.execute(text(ins), **ins_args)
                image.view_count += 1
                image.checkout_expire = None
                db.session.add(image)
                db.session.commit()

                flash("Transcription saved!", "saved")

            # clear form fields
            for field in form:
                if field.type != 'CSRFTokenField':
                    field.data = None
        else:
            return render_template('transcribe.html', form=form, task=task_dict, is_new=False)

    else:
        form = form(meta={})

    # This is where we put in the image. 
    # Right now it's just always loading the example image
    # flask_session['image'] = task.sample_image
    # flask_session['image_type'] = task.sample_image.rsplit('.', 1)[1].lower()
    image_id = request.args.get('image_id')

    image = None

    if image_id:
        image = db.session.query(Image).get(int(image_id))
    else:
        # add in a filter so that one user does not review the same image multiple times
        # images left & images total (for progress bar) should be specific to the user
        image = db.session.query(Image)\
                .filter(Image.form_id == task.id)\
                .filter(Image.checkout_expire == None)\
                .filter(Image.view_count < task_dict['reviewer_count'])\
                .order_by(Image.view_count)\
                .first()

    # task_dict['images_left'] = db.session.query(Image)\
    #         .filter(Image.form_id == task.id)\
    #         .filter(Image.view_count < task_dict['reviewer_count'])\
    #         .count()
    # task_dict['images_total'] = db.session.query(Image)\
    #         .filter(Image.form_id == task.id)\
    #         .count()

    if current_user.is_anonymous():
        username = request.remote_addr
    else:
        username = current_user.name
    q = ''' 
        SELECT * from "{0}" where transcriber = '{1}'
        '''.format(task_dict['table_name'], username)
    with engine.begin() as conn:
        user_transcriptions = conn.execute(text(q)).fetchall()
    task_dict['user_transcriptions'] = len(user_transcriptions)


    if image == None:
        if task_dict['images_left'] == 0:
            flash('No more documents left to transcribe for %s!' %task_dict['name'])
            return redirect(url_for('views.index'))
        else:
            flash("All images associated with '%s' have been checked out" %task_dict['name'])
            return redirect(url_for('views.index'))
    else:
        # checkout image for 5 mins
        image.checkout_expire = expire_time
        db.session.add(image)
        db.session.commit()
        flask_session['image'] = image.fetch_url
        flask_session['image_type'] = image.image_type
        flask_session['image_id'] = image.id
        return render_template('transcribe.html', form=form, task=task_dict, is_new = True)

@views.route('/download-transcriptions/', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def download_transcriptions():
    if not request.args.get('task_id'):
        return redirect(url_for('views.index'))

    task = db.session.query(FormMeta)\
            .filter(FormMeta.id == request.args['task_id'])\
            .first()
    task_dict = task.as_dict()
    table_name = task_dict['table_name']

    copy = '''
        COPY (
          SELECT * from "{0}"
        ) TO STDOUT WITH CSV HEADER DELIMITER ','
    '''.format(table_name)

    engine = db.session.bind
    conn = engine.raw_connection()
    curs = conn.cursor()
    outp = StringIO()
    curs.copy_expert(copy, outp)
    outp.seek(0)
    resp = make_response(outp.getvalue())
    resp.headers['Content-Type'] = 'text/csv'
    filedate = datetime.now().strftime('%Y-%m-%d')
    resp.headers['Content-Disposition'] = 'attachment; filename=transcriptions_{0}_{1}.csv'.format(task_dict['slug'], filedate)
    return resp

@views.route('/transcriptions/', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def transcriptions():
    if not request.args.get('task_id'):
        return redirect(url_for('views.index'))
    transcriptions = None
    header = None
    task_id = request.args.get('task_id')

    task = db.session.query(FormMeta)\
            .filter(FormMeta.id == task_id)\
            .first()
    task_dict = task.as_dict()

    table_name = task_dict['table_name']

    images_unseen = db.session.query(Image)\
            .filter(Image.form_id == task_id)\
            .filter(Image.view_count == 0)\
            .all()

    q = ''' 
            SELECT * from (SELECT id, fetch_url from image) i
            JOIN "{0}" t 
            ON (i.id = t.image_id)
            ORDER BY i.id, t.id
        '''.format(table_name)
    h = ''' 
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = '{0}'
        '''.format(table_name)

    engine = db.session.bind
    with engine.begin() as conn:
        t_header = conn.execute(text(h)).fetchall()
        rows_all = conn.execute(text(q)).fetchall()

    if len(rows_all) > 0:
        transcriptions = pretty_transcriptions(t_header, rows_all, task_id)

    return render_template('transcriptions.html', task=task_dict, transcriptions=transcriptions, images_unseen=images_unseen)

@views.route('/user/', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def user():
    if not request.args.get('user'):
        return redirect(url_for('views.index'))

    user_transcriptions = []
    user_row = db.session.query(User)\
                .filter(User.name == request.args.get('user'))\
                .first()
    
    if user_row:
        user = {
        'id': user_row.id,
        'name': user_row.name,
        'detail': user_row.email
        }
    else:
        user = {
        'id': None,
        'name': request.args.get('user'),
        'detail': "Anonymous Transcriber"
        }

    all_tasks = db.session.query(FormMeta)\
            .filter(or_(FormMeta.status != 'deleted', 
                        FormMeta.status == None)).all()

    engine = db.session.bind

    for task in all_tasks:
        task_info = task.as_dict()
        table_name = task_info['table_name']

        q = ''' 
                SELECT * from (SELECT id, fetch_url from image) i
                JOIN "{0}" t 
                ON (i.id = t.image_id)
                WHERE transcriber = '{1}'
            '''.format(table_name, user['name'])
        h = ''' 
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = '{0}'
        '''.format(table_name)

        with engine.begin() as conn:
            t_header = conn.execute(text(h)).fetchall()
            rows_all = conn.execute(text(q)).fetchall()

        if len(rows_all) > 0:
            transcriptions = pretty_transcriptions(t_header, rows_all, task_info["id"])
            user_transcriptions.append((task_info, transcriptions))

    return render_template('user.html', user=user, user_transcriptions = user_transcriptions)

@views.route('/uploads/<filename>')
def uploaded_image(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@views.route('/viewer/')
def viewer():
    return render_template('viewer.html')
